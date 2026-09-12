"""Uploaded images remain active until an explicit context boundary removes them."""

import pytest

from ricky.agent.context import (
    assemble_context,
    inspect_context,
    source_history_digest,
    validate_user_images,
)
from ricky.agent.session import (
    AgentSession,
    ContextCheckpoint,
    MediaAdmissionEvidence,
    SessionMediaRecord,
)
from ricky.config import RickySettings
from ricky.llm import ImagePart, Message, UserContent
from ricky.profiles import ProfileLabel
from ricky.tools import ToolRegistry


def _session(limit: int = 20) -> AgentSession:
    settings = RickySettings.model_validate({"context": {"media": {"request_image_limit": limit}}})
    return AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())


def _image(session: AgentSession, index: int, *, upload: bool = True) -> ImagePart:
    record = SessionMediaRecord(
        id=f"media_{index:032x}",
        byte_count=100,
        sha256=f"{index:064x}",
        width=10,
        height=5,
        source_label=ProfileLabel.owned_by("personal"),
        relative_path=f"media_{index:032x}.png",
        provenance="user_upload" if upload else "browser_screenshot",
        retention="session",
        admission=MediaAdmissionEvidence(
            disclosure_class="user_upload",
            admitted_provider=session.provider,
            source_owner="personal",
        ),
    )
    session.media.append(record)
    return ImagePart(artifact=record.reference())


def _projected(session: AgentSession) -> list[ImagePart]:
    request = assemble_context(session, ToolRegistry([]), turn_id="test", iteration=1).request
    return [
        part
        for message in request.messages
        for part in message.content
        if isinstance(part, ImagePart)
    ]


def test_uploads_survive_multiple_turns_and_browser_suffix_projection() -> None:
    session = _session()
    uploads = [_image(session, index) for index in range(1, 5)]
    browser = [_image(session, index, upload=False) for index in range(5, 9)]
    for part in [*uploads, *browser]:
        session.history.extend(
            [Message(role="user", content=[part]), Message.text("assistant", "seen")]
        )
    session = AgentSession.model_validate_json(session.model_dump_json())
    assert _projected(session) == [*uploads, *browser[-2:]]


def test_uploads_are_rejected_as_complete_set_and_draft_is_not_mutated() -> None:
    session = _session(2)
    images = [_image(session, index) for index in range(1, 4)]
    session.history = [Message(role="user", content=[*images[:2]])]
    draft = UserContent(parts=[images[2]])
    original = session.model_dump_json()
    with pytest.raises(ValueError, match="compact or clear"):
        validate_user_images(session, draft)
    assert session.model_dump_json() == original
    assert draft.parts == [images[2]]
    session.history.append(Message(role="user", content=[images[2]]))
    with pytest.raises(ValueError, match="request image count"):
        _projected(session)
    # Compaction and diagnostics can inspect over-limit context without sending pixels.
    report = inspect_context(session, ToolRegistry([]), enforce_char_limit=False)
    assert report.projected_image_count == 3


def test_compacted_uploads_leave_projection_but_remain_canonical_evidence() -> None:
    session = _session()
    first, second = _image(session, 1), _image(session, 2)
    session.history = [
        Message(role="user", content=[first]),
        Message.text("assistant", "seen"),
        Message(role="user", content=[second]),
    ]
    checkpoint = ContextCheckpoint(
        id="checkpoint_" + "1" * 32,
        summary="Earlier image was discussed.",
        covered_message_count=2,
        retained_from_message=2,
        source_digest=source_history_digest(session.history[:2]),
        estimated_tokens_before=100,
        estimated_tokens_after=50,
    )
    session.checkpoints.append(checkpoint)
    session.active_checkpoint_id = checkpoint.id
    assert _projected(session) == [second]
    assert session.history[0].content == [first]
    assert len(session.media) == 2


def test_upload_message_limit_is_ten_even_with_larger_context_limit() -> None:
    session = _session()
    images = [_image(session, index) for index in range(1, 12)]
    validate_user_images(session, UserContent(parts=[*images[:10]]))
    with pytest.raises(ValueError, match="at most 10"):
        validate_user_images(session, UserContent(parts=[*images]))


async def test_model_capability_preflight_rejects_known_text_only_without_stream() -> None:
    from collections.abc import AsyncIterator

    from ricky.agent import AgentLoop
    from ricky.llm import CompletionRequest, ModelInfo, StreamEvent, UnsupportedInputModalityError

    class CatalogProvider:
        name = "catalog"
        catalog_calls = 0

        async def list_models(self) -> list[ModelInfo]:
            self.catalog_calls += 1
            return [ModelInfo(id=session.model)]

        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            raise AssertionError("Rejected image must never reach the stream")
            yield  # pragma: no cover

        async def aclose(self) -> None:
            pass

    settings = RickySettings()
    session = _session()
    provider = CatalogProvider()
    loop = AgentLoop(provider=provider, registry=ToolRegistry([]), settings=settings)
    await loop.validate_image_input(session, UserContent.text("hello"))
    assert provider.catalog_calls == 0
    draft = UserContent(parts=[_image(session, 1)])
    before = session.model_dump_json()
    with pytest.raises(UnsupportedInputModalityError, match="does not support image input"):
        await loop.validate_image_input(session, draft)
    assert provider.catalog_calls == 1
    assert session.model_dump_json() == before
    events = [event async for event in loop.run_turn(session, draft)]
    assert events[-1].kind == "turn_finished"
    assert session.history == []


async def test_image_preflight_checks_full_model_budget_before_provider_call() -> None:
    from collections.abc import AsyncIterator

    from ricky.agent import AgentLoop
    from ricky.agent.context_types import SessionModelContext
    from ricky.llm import CompletionRequest, StreamEvent

    class NoCallProvider:
        name = "no-call"

        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            raise AssertionError("Over-budget image input must not be sent")
            yield  # pragma: no cover

        async def aclose(self) -> None:
            pass

    session = _session()
    session.model_context = SessionModelContext(context_window_tokens=100, source="configured")
    loop = AgentLoop(provider=NoCallProvider(), registry=ToolRegistry([]), settings=RickySettings())
    draft = UserContent(parts=[_image(session, 1)])
    before = session.model_dump_json()
    with pytest.raises(ValueError, match="available input budget"):
        await loop.validate_image_input(session, draft)
    assert session.model_dump_json() == before
