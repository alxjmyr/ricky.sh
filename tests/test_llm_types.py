"""Tests for canonical LLM types."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from ricky.llm._assembly import ToolCallState, assembled_message
from ricky.llm.types import (
    CompletionRequest,
    ImagePart,
    MediaArtifactRef,
    Message,
    MessageDone,
    StreamEvent,
    TextDelta,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
    UserContent,
)
from ricky.profiles import ProfileLabel


def _image_ref() -> MediaArtifactRef:
    return MediaArtifactRef(
        id="media_" + "a" * 32,
        byte_count=123,
        sha256="b" * 64,
        width=4,
        height=3,
        source_label=ProfileLabel.owned_by("personal"),
    )


def test_message_content_parts_round_trip_json() -> None:
    message = Message(
        role="assistant",
        content=[
            ThinkingPart(text="checking"),
            TextPart(text="Use the file tool."),
            ToolCallPart(id="call_1", name="read_file", args={"path": "README.md"}),
        ],
    )

    restored = Message.model_validate_json(message.model_dump_json())

    assert restored == message
    assert isinstance(restored.content[2], ToolCallPart)


def test_tool_result_message_round_trip_json() -> None:
    message = Message(
        role="tool",
        content=[ToolResultPart(call_id="call_1", content="file contents", is_error=False)],
    )

    restored = Message.model_validate_json(message.model_dump_json())

    assert restored == message


def test_canonical_user_image_content_is_ordered_strict_and_path_free() -> None:
    content = UserContent(
        parts=[
            TextPart(text="before"),
            ImagePart(artifact=_image_ref()),
            TextPart(text="after"),
        ]
    )

    restored = UserContent.model_validate_json(content.model_dump_json())
    serialized = content.model_dump_json()

    assert restored == content
    assert [part.kind for part in restored.parts] == ["text", "image", "text"]
    assert "path" not in serialized
    assert "provider" not in serialized
    assert "retention" not in serialized
    assert "browser" not in serialized
    assert "base64" not in serialized

    with pytest.raises(ValidationError):
        MediaArtifactRef.model_validate(
            {**_image_ref().model_dump(mode="python"), "path": "/private/image.png"}
        )
    with pytest.raises(ValidationError):
        Message(role="assistant", content=[ImagePart(artifact=_image_ref())])
    with pytest.raises(ValidationError):
        MediaArtifactRef.model_validate({**_image_ref().model_dump(mode="python"), "width": 0})
    with pytest.raises(ValidationError):
        MediaArtifactRef.model_validate(
            {**_image_ref().model_dump(mode="python"), "byte_count": "123"}
        )


def test_completion_request_round_trip_json() -> None:
    request = CompletionRequest(
        model="provider/model",
        messages=[Message.text("user", "hello")],
        session_id="session_round_trip",
        tools=[
            ToolSpec(
                name="read_file",
                description="Read a file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            )
        ],
        temperature=0.2,
        max_tokens=200,
        provider_options={"require_parameters": True},
    )

    restored = CompletionRequest.model_validate_json(request.model_dump_json())

    assert restored == request


def test_stream_event_union_round_trip_json() -> None:
    adapter = TypeAdapter(StreamEvent)
    event = MessageDone(
        message=Message(role="assistant", content=[TextPart(text="done")]),
        usage=Usage(prompt_tokens=3, completion_tokens=5),
        stop_reason="stop",
    )

    dumped = adapter.dump_json(event)
    restored = adapter.validate_json(dumped)

    assert restored == event
    assert Usage(prompt_tokens=3, completion_tokens=5).total_tokens == 8


def test_text_delta_union_validation() -> None:
    adapter = TypeAdapter(StreamEvent)
    restored = adapter.validate_python({"kind": "text_delta", "delta": "hi"})

    assert restored == TextDelta(delta="hi")


def test_stream_assembly_preserves_top_level_tool_argument_json_errors() -> None:
    malformed = assembled_message(
        "",
        "",
        {0: ToolCallState(id="call_1", name="probe", args_json="{bad")},
    )
    non_object = assembled_message(
        "",
        "",
        {0: ToolCallState(id="call_2", name="probe", args_json="[]")},
    )

    malformed_call = malformed.content[0]
    non_object_call = non_object.content[0]
    assert isinstance(malformed_call, ToolCallPart)
    assert isinstance(non_object_call, ToolCallPart)
    assert malformed_call.argument_error == "malformed JSON"
    assert non_object_call.argument_error == "non-object JSON"
