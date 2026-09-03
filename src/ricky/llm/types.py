"""Canonical LLM types used across provider boundaries."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.profiles import ProfileLabel

Role = Literal["system", "user", "assistant", "tool"]


class TextPart(BaseModel):
    """Plain natural-language content."""

    kind: Literal["text"] = "text"
    text: str


class ThinkingPart(BaseModel):
    """Provider reasoning content, when exposed by an adapter."""

    kind: Literal["thinking"] = "thinking"
    text: str


class ToolCallPart(BaseModel):
    """A model-requested tool call."""

    kind: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    argument_error: Literal["malformed JSON", "non-object JSON"] | None = None


class ToolArtifactRef(BaseModel):
    """Provider-safe reference to one immutable session tool-result artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")
    call_id: str = Field(min_length=1, max_length=500)
    tool_name: str = Field(min_length=1, max_length=200)
    full_chars: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["text/plain"] = "text/plain"
    excerpt_chars: int = Field(ge=0)


class MediaArtifactRef(BaseModel):
    """Provider-neutral identity for immutable session media."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^media_[0-9a-f]{32}$")
    media_type: Literal["image/png"] = "image/png"
    byte_count: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    source_label: ProfileLabel


class ImagePart(BaseModel):
    """One canonical image input backed by an immutable media reference."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["image"] = "image"
    artifact: MediaArtifactRef


class ToolResultPart(BaseModel):
    """A tool result supplied back to the model."""

    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str
    is_error: bool = False
    artifact: ToolArtifactRef | None = None


ContentPart = Annotated[
    TextPart | ThinkingPart | ToolCallPart | ToolResultPart | ImagePart,
    Field(discriminator="kind"),
]

UserContentPart = Annotated[TextPart | ImagePart, Field(discriminator="kind")]


class UserContent(BaseModel):
    """Ordered canonical content accepted from any authenticated producer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    parts: list[UserContentPart] = Field(min_length=1, max_length=100)

    @classmethod
    def text(cls, value: str) -> UserContent:
        """Adapt one existing text-only input into canonical user content."""
        return cls(parts=[TextPart(text=value)])

    def display_text(self) -> str:
        """Return safe text for interfaces and typed turn-start evidence."""
        text = "\n".join(part.text for part in self.parts if isinstance(part, TextPart))
        image_count = sum(isinstance(part, ImagePart) for part in self.parts)
        marker = f"[{image_count} image{'s' if image_count != 1 else ''}]" if image_count else ""
        return "\n".join(item for item in (text, marker) if item)


class Message(BaseModel):
    """A canonical chat message independent of provider wire formats."""

    role: Role
    content: list[ContentPart] = Field(default_factory=list)

    @model_validator(mode="after")
    def _images_are_user_input(self) -> Message:
        if self.role != "user" and any(isinstance(part, ImagePart) for part in self.content):
            raise ValueError("image parts are allowed only in user messages")
        return self

    @classmethod
    def text(cls, role: Role, text: str) -> Message:
        """Create a message with one text part."""
        return cls(role=role, content=[TextPart(text=text)])


class ToolSpec(BaseModel):
    """Provider-neutral tool schema."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class CompletionRequest(BaseModel):
    """A provider-neutral streaming completion request."""

    model: str
    messages: list[Message]
    session_id: str | None = None
    tools: list[ToolSpec] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    """Provider-neutral model catalog entry."""

    id: str
    name: str | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    input_modalities: list[Literal["text", "image"]] = Field(default_factory=lambda: ["text"])

    @field_validator("input_modalities")
    @classmethod
    def _unique_input_modalities(
        cls, values: list[Literal["text", "image"]]
    ) -> list[Literal["text", "image"]]:
        if not values or "text" not in values:
            raise ValueError("model input modalities must include text")
        if len(values) != len(set(values)):
            raise ValueError("model input modalities must be unique")
        return values


class Usage(BaseModel):
    """Token usage reported by a provider."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Total prompt plus completion tokens."""
        return self.prompt_tokens + self.completion_tokens


class TextDelta(BaseModel):
    """A streamed text delta."""

    kind: Literal["text_delta"] = "text_delta"
    delta: str


class ThinkingDelta(BaseModel):
    """A streamed reasoning delta."""

    kind: Literal["thinking_delta"] = "thinking_delta"
    delta: str


class ToolCallDelta(BaseModel):
    """A streamed tool-call delta."""

    kind: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    id: str | None = None
    name: str | None = None
    args_delta: str = ""


class MessageDone(BaseModel):
    """The final assembled assistant message for a stream."""

    kind: Literal["message_done"] = "message_done"
    message: Message
    usage: Usage = Field(default_factory=Usage)
    stop_reason: str | None = None


StreamEvent = Annotated[
    TextDelta | ThinkingDelta | ToolCallDelta | MessageDone,
    Field(discriminator="kind"),
]


class ProviderError(Exception):
    """Base class for typed provider failures."""


class AuthError(ProviderError):
    """Authentication or authorization failed."""


class RateLimitError(ProviderError):
    """The provider rejected the request because of rate limiting."""


class ContextLengthError(ProviderError):
    """The request is too large for the target model context window."""


class UnsupportedInputModalityError(ProviderError):
    """The target provider or model cannot accept one canonical input modality."""


class TransportError(ProviderError):
    """Network, protocol, or server transport failure."""
