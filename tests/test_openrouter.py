"""Tests for the OpenRouter adapter."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ricky.config import RickySettings
from ricky.llm.openrouter import (
    OpenRouterProvider,
    to_openrouter_request,
    to_openrouter_request_with_media,
)
from ricky.llm.provider import ResolvedMedia, collect
from ricky.llm.types import (
    AuthError,
    CompletionRequest,
    ImagePart,
    MediaArtifactRef,
    Message,
    RateLimitError,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    TransportError,
    UnsupportedInputModalityError,
)
from ricky.profiles import ProfileLabel


def _settings() -> RickySettings:
    return RickySettings(openrouter_api_key=SecretStr("test-key"), request_timeout_seconds=5)


def _sse(*payloads: dict[str, Any]) -> bytes:
    lines = [f"data: {json.dumps(payload)}" for payload in payloads]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _image_ref() -> MediaArtifactRef:
    return MediaArtifactRef(
        id="media_" + "a" * 32,
        byte_count=7,
        sha256="b" * 64,
        width=2,
        height=1,
        source_label=ProfileLabel.owned_by("personal"),
    )


@pytest.mark.asyncio
async def test_openrouter_image_translation_resolves_bytes_at_wire_boundary_in_order() -> None:
    seen: list[object] = []

    class Resolver:
        async def resolve(self, reference: object) -> ResolvedMedia:
            seen.append(reference)
            return ResolvedMedia(
                media_type="image/png",
                content=b"pngbody",
                sha256="b" * 64,
                width=2,
                height=1,
            )

    request = CompletionRequest(
        model="provider/image-model",
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

    payload = await to_openrouter_request_with_media(request, Resolver())

    assert seen == [_image_ref()]
    assert payload["messages"][0]["content"] == [
        {"type": "text", "text": "before"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,cG5nYm9keQ=="},
        },
        {"type": "text", "text": "after"},
    ]
    assert "/private" not in json.dumps(payload)

    with pytest.raises(UnsupportedInputModalityError):
        await to_openrouter_request_with_media(request, None)


def test_to_openrouter_request_translates_messages_and_tools() -> None:
    request = CompletionRequest(
        model="provider/model",
        session_id="session_internal_only",
        messages=[
            Message.text("system", "be terse"),
            Message.text("user", "read the file"),
            Message(
                role="assistant",
                content=[
                    TextPart(text="I'll read it."),
                    ToolCallPart(id="call_1", name="read_file", args={"path": "README.md"}),
                ],
            ),
            Message(
                role="tool",
                content=[ToolResultPart(call_id="call_1", content="contents")],
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
        max_tokens=50,
        provider_options={"provider": {"order": ["anthropic"]}},
    )

    payload = to_openrouter_request(request)

    assert payload["model"] == "provider/model"
    assert payload["stream"] is True
    assert payload["temperature"] == 0.1
    assert payload["max_tokens"] == 50
    assert payload["provider"] == {"order": ["anthropic"]}
    assert "session_id" not in payload
    assert (
        payload["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"path":"README.md"}'
    )
    assert payload["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "contents",
    }
    assert payload["tools"][0]["function"] == {
        "name": "read_file",
        "description": "Read a file",
        "parameters": {"type": "object"},
    }


@pytest.mark.asyncio
async def test_model_catalog_projects_advertised_input_modalities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vision-model",
                        "name": "Vision",
                        "architecture": {"input_modalities": ["image", "text"]},
                    },
                    {
                        "id": "unknown-model",
                        "architecture": {"input_modalities": ["audio"]},
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await OpenRouterProvider(_settings(), client=client).list_models()

    assert models[0].input_modalities == ["text", "image"]
    assert models[1].input_modalities == ["text"]


@pytest.mark.asyncio
async def test_stream_reassembles_tool_call_and_sends_tool_result_back() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.headers["authorization"] == "Bearer test-key"

        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "content": "I'll inspect that.",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_read",
                                            "type": "function",
                                            "function": {
                                                "name": "read_file",
                                                "arguments": '{"path"',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": ':"README.md"}'},
                                        }
                                    ]
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    },
                ),
            )

        assert payload["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call_read",
            "content": "README contents",
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"choices": [{"delta": {"content": "The README is concise."}}]},
                {
                    "choices": [{"finish_reason": "stop", "delta": {}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 6},
                },
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(
            _settings(),
            client=client,
            max_retries=1,
            retry_base_seconds=0,
        )
        first_request = CompletionRequest(
            model="provider/model",
            messages=[Message.text("user", "read README.md")],
            tools=[
                ToolSpec(
                    name="read_file",
                    description="Read a file",
                    parameters={"type": "object"},
                )
            ],
        )

        first_message, first_usage = await collect(provider.stream(first_request))

        assert first_usage.prompt_tokens == 11
        assert first_usage.completion_tokens == 7
        assert isinstance(first_message.content[-1], ToolCallPart)
        assert first_message.content[-1].id == "call_read"
        assert first_message.content[-1].name == "read_file"
        assert first_message.content[-1].args == {"path": "README.md"}

        second_request = CompletionRequest(
            model="provider/model",
            messages=[
                Message.text("user", "read README.md"),
                first_message,
                Message(
                    role="tool",
                    content=[ToolResultPart(call_id="call_read", content="README contents")],
                ),
            ],
        )
        second_message, second_usage = await collect(provider.stream(second_request))

    assert second_message == Message.text("assistant", "The README is concise.")
    assert second_usage.total_tokens == 26
    assert len(requests) == 2
    assert requests[0]["tools"][0]["function"]["name"] == "read_file"


@pytest.mark.asyncio
async def test_stream_preserves_provider_stringified_nested_tool_object() -> None:
    nested = json.dumps({"task_id": "task_" + "a" * 32, "profile": "personal"})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_nested",
                                        "function": {
                                            "name": "probe",
                                            "arguments": json.dumps({"payload": nested}),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(_settings(), client=client, max_retries=1)
        message, _ = await collect(
            provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "probe")]))
        )

    call = message.content[0]
    assert isinstance(call, ToolCallPart)
    assert call.args == {"payload": nested}
    assert call.argument_error is None


@pytest.mark.asyncio
async def test_http_errors_map_to_typed_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error":"bad key"}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(_settings(), client=client, max_retries=1)
        with pytest.raises(AuthError):
            await collect(
                provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "hi")]))
            )


@pytest.mark.asyncio
async def test_image_model_rejection_is_typed_and_not_retried() -> None:
    attempts = 0

    class Resolver:
        async def resolve(self, reference: object) -> ResolvedMedia:
            del reference
            return ResolvedMedia("image/png", b"pngbody", "b" * 64, 2, 1)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, content=b"model does not support image input modality")

    request = CompletionRequest(
        model="text-only",
        messages=[Message(role="user", content=[ImagePart(artifact=_image_ref())])],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(
            _settings(),
            client=client,
            max_retries=3,
            retry_base_seconds=0,
            media_resolver=Resolver(),
        )
        with pytest.raises(UnsupportedInputModalityError):
            await collect(provider.stream(request))

    assert attempts == 1


@pytest.mark.asyncio
async def test_retries_rate_limit_then_succeeds() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, content=b"rate limited")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"choices": [{"delta": {"content": "ok"}}]}),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(
            _settings(),
            client=client,
            max_retries=2,
            retry_base_seconds=0,
        )
        message, _usage = await collect(
            provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "hi")]))
        )

    assert attempts == 2
    assert message == Message.text("assistant", "ok")


@pytest.mark.asyncio
async def test_rate_limit_raises_after_retries_exhausted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"rate limited")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(
            _settings(),
            client=client,
            max_retries=1,
            retry_base_seconds=0,
        )
        with pytest.raises(RateLimitError):
            await collect(
                provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "hi")]))
            )


@pytest.mark.asyncio
async def test_mid_stream_failure_is_not_retried_after_output() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\ndata: not-json\n\n'),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(
            _settings(),
            client=client,
            max_retries=3,
            retry_base_seconds=0,
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
async def test_stream_ending_without_done_marker_is_a_transport_error() -> None:
    """A clean EOF before [DONE] is a truncated response, not a complete message."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(_settings(), client=client, max_retries=1)
        with pytest.raises(TransportError, match="ended before completion"):
            await collect(
                provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "hi")]))
            )


@pytest.mark.asyncio
async def test_malformed_stream_raises_transport_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: not-json\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(_settings(), client=client, max_retries=1)
        with pytest.raises(TransportError):
            await collect(
                provider.stream(CompletionRequest(model="m", messages=[Message.text("user", "hi")]))
            )
