"""Tests for the Anthropic Messages API adapter."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ricky.config import AnthropicSettings, ProvidersSettings, RickySettings
from ricky.llm.anthropic import (
    AnthropicProvider,
    to_anthropic_request,
    to_anthropic_request_with_media,
)
from ricky.llm.provider import ResolvedMedia, collect
from ricky.llm.types import (
    AuthError,
    CompletionRequest,
    ContextLengthError,
    ImagePart,
    MediaArtifactRef,
    Message,
    RateLimitError,
    TextDelta,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    TransportError,
    UnsupportedInputModalityError,
)
from ricky.profiles import ProfileLabel


def _settings(*, default_max_tokens: int = 4096) -> RickySettings:
    return RickySettings(
        anthropic_api_key=SecretStr("test-key"),
        request_timeout_seconds=5,
        providers=ProvidersSettings(
            anthropic=AnthropicSettings(default_max_tokens=default_max_tokens)
        ),
    )


def _sse(*payloads: dict[str, Any]) -> bytes:
    frames = [f"event: {payload['type']}\ndata: {json.dumps(payload)}" for payload in payloads]
    return ("\n\n".join(frames) + "\n\n").encode()


def _text_stream(text: str = "ok") -> bytes:
    return _sse(
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 3, "output_tokens": 0}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 2},
        },
        {"type": "message_stop"},
    )


def _image_ref() -> MediaArtifactRef:
    return MediaArtifactRef(
        id="media_" + "c" * 32,
        byte_count=7,
        sha256="d" * 64,
        width=2,
        height=1,
        source_label=ProfileLabel.owned_by("personal"),
    )


@pytest.mark.asyncio
async def test_anthropic_image_translation_resolves_bytes_at_wire_boundary_in_order() -> None:
    class Resolver:
        async def resolve(self, reference: object) -> ResolvedMedia:
            assert reference == _image_ref()
            return ResolvedMedia(
                media_type="image/png",
                content=b"pngbody",
                sha256="d" * 64,
                width=2,
                height=1,
            )

    request = CompletionRequest(
        model="claude-image",
        messages=[
            Message(
                role="user",
                content=[
                    TextPart(text="before"),
                    ImagePart(artifact=_image_ref()),
                    TextPart(text="after"),
                ],
            )
        ],
    )

    payload = await to_anthropic_request_with_media(request, Resolver())

    assert payload["messages"][0]["content"] == [
        {"type": "text", "text": "before"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "cG5nYm9keQ==",
            },
        },
        {"type": "text", "text": "after"},
    ]
    assert "/private" not in json.dumps(payload)
    with pytest.raises(UnsupportedInputModalityError):
        await to_anthropic_request_with_media(request, None)


def test_to_anthropic_request_translates_system_tools_and_results() -> None:
    request = CompletionRequest(
        model="claude-test",
        messages=[
            Message.text("system", "be terse"),
            Message.text("user", "read the file"),
            Message(
                role="assistant",
                content=[
                    ThinkingPart(text="private reasoning is not replayed"),
                    TextPart(text="I'll read it."),
                    ToolCallPart(id="call_1", name="read_file", args={"path": "README.md"}),
                ],
            ),
            Message(
                role="tool",
                content=[
                    ToolResultPart(
                        call_id="call_1",
                        content="contents",
                        is_error=False,
                    )
                ],
            ),
        ],
        tools=[
            ToolSpec(
                name="read_file",
                description="Read a file",
                parameters={"type": "object"},
            )
        ],
        temperature=0.1,
    )

    payload = to_anthropic_request(request, default_max_tokens=4096)

    assert payload["model"] == "claude-test"
    assert payload["system"] == "be terse"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 4096
    assert payload["temperature"] == 0.1
    assert payload["messages"][1]["content"] == [
        {"type": "text", "text": "I'll read it."},
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
    ]
    assert payload["messages"][2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "contents",
                "is_error": False,
            }
        ],
    }
    assert payload["tools"] == [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object"},
        }
    ]


def test_request_max_tokens_and_provider_options_override_defaults() -> None:
    request = CompletionRequest(
        model="claude-test",
        messages=[Message.text("user", "hello")],
        session_id="session_internal_only",
        max_tokens=100,
        provider_options={"max_tokens": 50, "thinking": {"type": "enabled", "budget_tokens": 20}},
    )

    payload = to_anthropic_request(request, default_max_tokens=4096)

    assert payload["max_tokens"] == 50
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 20}
    assert "session_id" not in payload


@pytest.mark.asyncio
async def test_stream_reassembles_thinking_text_tool_call_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 4096
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 11, "output_tokens": 0}},
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "inspect"},
                },
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "I'll inspect."},
                },
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_read",
                        "name": "read_file",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path"'},
                },
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {"type": "input_json_delta", "partial_json": ':"README.md"}'},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 7},
                },
                {"type": "message_stop"},
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(_settings(), client=client, max_retries=1)
        message, usage = await collect(
            provider.stream(
                CompletionRequest(
                    model="claude-test",
                    messages=[Message.text("user", "read README")],
                )
            )
        )

    assert isinstance(message.content[0], ThinkingPart)
    assert isinstance(message.content[1], TextPart)
    assert isinstance(message.content[2], ToolCallPart)
    assert message.content[2].id == "call_read"
    assert message.content[2].name == "read_file"
    assert message.content[2].args == {"path": "README.md"}
    assert usage.prompt_tokens == 11
    assert usage.completion_tokens == 7


@pytest.mark.asyncio
async def test_stream_preserves_provider_stringified_nested_tool_object() -> None:
    nested = json.dumps({"task_id": "task_" + "a" * 32, "profile": "personal"})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 1, "output_tokens": 0}},
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_nested",
                        "name": "probe",
                        "input": {"payload": nested},
                    },
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(_settings(), client=client, max_retries=1)
        message, _ = await collect(
            provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "probe")]))
        )

    call = message.content[0]
    assert isinstance(call, ToolCallPart)
    assert call.args == {"payload": nested}
    assert call.argument_error is None


@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (401, "bad key", AuthError),
        (403, "forbidden", AuthError),
        (429, "slow down", RateLimitError),
        (400, "prompt is too long", ContextLengthError),
        (529, "overloaded", TransportError),
        (500, "server failed", TransportError),
    ],
)
@pytest.mark.asyncio
async def test_http_errors_map_to_typed_errors(
    status: int,
    body: str,
    error_type: type[Exception],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(_settings(), client=client, max_retries=1)
        with pytest.raises(error_type):
            await collect(
                provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "hi")]))
            )


@pytest.mark.asyncio
async def test_stream_image_rejection_maps_to_typed_modality_error() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "This model does not support image input modality",
                    },
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(_settings(), client=client, max_retries=3)
        with pytest.raises(UnsupportedInputModalityError):
            await collect(
                provider.stream(
                    CompletionRequest(model="text-only", messages=[Message.text("user", "hi")])
                )
            )
    assert attempts == 1


@pytest.mark.asyncio
async def test_retries_transport_failure_then_succeeds() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(529, content=b"overloaded")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_text_stream(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(
            _settings(), client=client, max_retries=2, retry_base_seconds=0
        )
        message, _usage = await collect(
            provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "hi")]))
        )

    assert attempts == 2
    assert message == Message.text("assistant", "ok")


@pytest.mark.asyncio
async def test_mid_stream_failure_is_not_retried_after_output() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'event: content_block_delta\ndata: {"type":"content_block_delta",'
                b'"index":0,"delta":{"type":"text_delta","text":"partial"}}\n\n'
                b"event: message_delta\ndata: not-json\n\n"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(
            _settings(), client=client, max_retries=3, retry_base_seconds=0
        )
        deltas: list[str] = []
        with pytest.raises(TransportError):
            async for event in provider.stream(
                CompletionRequest(model="m", messages=[Message.text("user", "hi")])
            ):
                if isinstance(event, TextDelta):
                    deltas.append(event.delta)

    assert attempts == 1
    assert deltas == ["partial"]


@pytest.mark.asyncio
async def test_stream_without_message_stop_is_malformed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"type": "ping"}),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(_settings(), client=client, max_retries=1)
        with pytest.raises(TransportError, match="message_stop"):
            await collect(
                provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "hi")]))
            )


@pytest.mark.asyncio
async def test_model_catalog_follows_pagination() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            assert request.url.params["limit"] == "100"
            assert "after_id" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-a", "display_name": "Claude A"}],
                    "has_more": True,
                    "last_id": "claude-a",
                },
            )
        assert request.url.params["after_id"] == "claude-a"
        return httpx.Response(
            200,
            json={
                "data": [{"id": "claude-b", "display_name": "Claude B"}],
                "has_more": False,
                "last_id": "claude-b",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(_settings(), client=client, max_retries=1)
        models = await provider.list_models()

    assert [model.id for model in models] == ["claude-a", "claude-b"]
    assert [model.name for model in models] == ["Claude A", "Claude B"]
    assert len(requests) == 2
