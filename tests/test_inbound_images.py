"""Inbound image download, durable album assembly, and gateway context regressions."""

from collections.abc import AsyncIterator
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from gateway_conversation_support import ScriptedProvider, _answer
from gateway_conversation_support import _settings as gateway_settings
from ricky.attachments import read_stored_attachment
from ricky.config import user_data_path
from ricky.gateway.conversations import ConversationCoordinator
from ricky.interfaces.messaging.telegram import TelegramTransport
from ricky.llm import CompletionRequest, ImagePart, MediaResolver, StreamEvent
from ricky.messaging.store import InboxLeaseError, MessagingStore
from ricky.messaging.types import ReceiveBatch, ReceivedImage
from test_messaging_store import MutableClock, _batch, _settings, _update
from test_telegram_transport import TOKEN
from test_telegram_transport import _settings as telegram_settings


class ResolvingProvider(ScriptedProvider):
    resolver: MediaResolver | None = None

    def bind_media_resolver(self, resolver: MediaResolver) -> None:
        self.resolver = resolver

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        images = [
            part
            for message in request.messages
            for part in message.content
            if isinstance(part, ImagePart)
        ]
        for part in images:
            assert self.resolver is not None
            assert (await self.resolver.resolve(part.artifact)).content == png()
        async for event in super().stream(request):
            yield event


def png(color: str = "red") -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), color).save(output, "PNG")
    return output.getvalue()


def image_update(
    clock: MutableClock,
    identifier: str,
    *,
    album: str | None = "album",
    content: bytes | None = None,
):
    update = _update(clock, identifier)
    return update.model_copy(
        update={
            "message": update.message.model_copy(update={"text": "", "media_group_id": album}),
            "images": [ReceivedImage(filename=f"{identifier}.png", content=content or png())],
        }
    )


async def test_authenticated_photo_download_and_image_only_message() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 2,
                                "from": {"id": 100},
                                "chat": {"id": 200},
                                "photo": [{"file_id": "small"}, {"file_id": "large"}],
                            },
                        }
                    ],
                },
            )
        if request.url.path.endswith("getFile"):
            assert b"large" in request.content
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/one.jpg"}})
        return httpx.Response(200, content=png())

    transport = TelegramTransport(
        "personal",
        telegram_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    batch = await transport.receive(None)
    await transport.aclose()
    assert calls == ["getUpdates", "getFile", "one.jpg"]
    assert batch.updates[0].message.text == ""
    assert batch.updates[0].images[0].content == png()
    assert TOKEN not in batch.model_dump_json()


async def test_unauthorized_photo_never_downloads() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("getUpdates")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 2,
                            "from": {"id": 999},
                            "chat": {"id": 200},
                            "photo": [{"file_id": "secret"}],
                        },
                    }
                ],
            },
        )

    transport = TelegramTransport(
        "personal",
        telegram_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    batch = await transport.receive(None)
    await transport.aclose()
    assert batch.updates[0].message.status == "rejected"
    assert not batch.updates[0].images


async def test_album_survives_restart_orders_images_and_deduplicates(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.project_data_dir = str(tmp_path / "project")
    clock = MutableClock()
    store = MessagingStore(settings, clock=clock)
    await store.initialize()
    first = image_update(clock, "2")
    await store.ingest(_batch(clock, "2").model_copy(update={"updates": [first]}))
    assert await store.list_pending_oldest() == []
    with pytest.raises(InboxLeaseError, match="collecting"):
        await store.claim_inbox(first.message.id, owner="early")
    clock.now += timedelta(seconds=1)
    store = MessagingStore(settings, clock=clock)
    second = image_update(clock, "1", content=png("blue"))
    batch = _batch(clock, "1").model_copy(update={"updates": [second]})
    await store.ingest(batch)
    await store.ingest(batch)
    clock.now += timedelta(seconds=3)
    messages = await store.list_pending_oldest()
    assert len(messages) == 1
    assert [image.filename for image in messages[0].images] == ["1.png", "2.png"]
    assert read_stored_attachment(messages[0].images[0], user_root=user_data_path(settings)) == png(
        "blue"
    )
    assert not (tmp_path / "project").exists()
    claim = await store.claim_inbox(first.message.id, owner="worker")
    await store.finish_inbox(claim, status="processed")
    await store.prune_inbox([first.message.id])
    assert not any((tmp_path / "user" / settings.messaging.attachment_dir).rglob("*.png"))


async def test_corrupt_album_member_rejects_whole_set_and_late_member_is_explicit(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    updates = [image_update(clock, "1"), image_update(clock, "2", content=b"broken")]
    await store.ingest(_batch(clock, "1", "2").model_copy(update={"updates": updates}))
    clock.now += timedelta(seconds=3)
    message = (await store.list_pending_oldest())[0]
    assert message.image_error and len(message.images) == 1
    claim = await store.claim_inbox(message.id, owner="worker")
    late = image_update(clock, "3")
    await store.ingest(_batch(clock, "3").model_copy(update={"updates": [late]}))
    assert "arrived after" in ((await store.get_inbox(late.message.id)).image_error or "")
    await store.finish_inbox(claim, status="processed")


async def test_gateway_image_only_and_restart_followup_keep_pixels(tmp_path: Path) -> None:
    settings = gateway_settings(tmp_path)
    clock = MutableClock()
    update = image_update(clock, "1", album=None)
    update = update.model_copy(
        update={"message": update.message.model_copy(update={"account": "personal/bot"})}
    )
    store = MessagingStore(settings)
    await store.initialize()
    await store.ingest(ReceiveBatch(transport="telegram", account="personal/bot", updates=[update]))
    providers = []

    def factory(*args):
        provider = ResolvingProvider([[_answer("An image.")]])
        providers.append(provider)
        return provider

    result = await ConversationCoordinator(settings, provider_factory=factory).process(
        update.message.id
    )
    assert result.status == "processed"
    assert any(
        isinstance(part, ImagePart)
        for message in providers[0].requests[0].messages
        for part in message.content
    )
    followup = _update(clock, "2")
    followup = followup.model_copy(
        update={
            "message": followup.message.model_copy(
                update={"account": "personal/bot", "text": "What color was it?"}
            )
        }
    )
    await store.ingest(
        ReceiveBatch(transport="telegram", account="personal/bot", updates=[followup])
    )
    await ConversationCoordinator(settings, provider_factory=factory).process(followup.message.id)
    assert any(
        isinstance(part, ImagePart)
        for message in providers[1].requests[0].messages
        for part in message.content
    )


async def test_same_batch_later_text_cannot_overtake_collecting_album(tmp_path: Path) -> None:
    clock = MutableClock()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    album = image_update(clock, "1")
    album = album.model_copy(
        update={
            "message": album.message.model_copy(
                update={"id": "inbound_ffffffffffffffffffffffffffffffff"}
            )
        }
    )
    later = _update(clock, "2")
    later = later.model_copy(
        update={
            "message": later.message.model_copy(
                update={"id": "inbound_00000000000000000000000000000000"}
            )
        }
    )
    await store.ingest(_batch(clock, "1", "2").model_copy(update={"updates": [album, later]}))
    assert await store.list_pending_oldest() == []
    clock.now += timedelta(seconds=3)
    assert [message.id for message in await store.list_pending_oldest()] == [
        album.message.id,
        later.message.id,
    ]


@pytest.mark.parametrize("invalid_member", ["caption", "video"])
async def test_authenticated_invalid_album_member_poison_entire_album(
    tmp_path: Path, invalid_member: str
) -> None:
    options = telegram_settings()
    second = {
        "message_id": 3,
        "from": {"id": 100},
        "chat": {"id": 200},
        "media_group_id": "one-album",
    }
    if invalid_member == "caption":
        second.update(
            photo=[{"file_id": "bad-caption"}],
            caption="x" * (options.max_inbound_text_length + 1),
        )
    else:
        second["video"] = {"file_id": "video"}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 2,
                                "from": {"id": 100},
                                "chat": {"id": 200},
                                "media_group_id": "one-album",
                                "photo": [{"file_id": "good"}],
                            },
                        },
                        {"update_id": 2, "message": second},
                    ],
                },
            )
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/a.jpg"}})
        return httpx.Response(200, content=png())

    clock = MutableClock()
    transport = TelegramTransport(
        "bot",
        options,
        clock=clock,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    batch = await transport.receive(None)
    await transport.aclose()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    await store.ingest(batch)
    clock.now += timedelta(seconds=3)
    pending = await store.list_pending_oldest()
    assert len(pending) == 1
    assert pending[0].image_error


async def test_archived_session_media_expires_only_after_retention(tmp_path: Path) -> None:
    from ricky.agent import AgentSession
    from ricky.media import SessionMediaStore, normalize_image_upload
    from ricky.profiles import ProfileScope
    from ricky.sessions import SessionStore

    settings = _settings(tmp_path)
    scope = ProfileScope.create("shared")
    session = AgentSession.create(settings, profile_scope=scope)
    media = SessionMediaStore.create(settings, session.id)
    records = await media.admit_images(
        session,
        images=[normalize_image_upload("one.png", png(), settings)],
        retention="conversation",
    )
    sessions = SessionStore(settings)
    await sessions.initialize()
    await sessions.create(session, scope=scope)
    assert not await sessions.prune_archived_media(session.id, scope=scope)
    assert (media.root / records[0].relative_path).exists()
    await sessions.archive(session.id, scope=scope, expected_revision=0)
    assert await sessions.prune_archived_media(session.id, scope=scope)
    assert not media.root.exists()
    assert not (await sessions.get(session.id, scope=scope)).session.media
    assert await sessions.prune_archived_media(session.id, scope=scope)


async def test_album_overflow_rejects_and_prune_removes_every_snapshot(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    clock = MutableClock()
    store = MessagingStore(settings, clock=clock)
    await store.initialize()
    ids = [str(index) for index in range(1, 12)]
    await store.ingest(
        _batch(clock, *ids).model_copy(
            update={"updates": [image_update(clock, identifier) for identifier in ids]}
        )
    )
    clock.now += timedelta(seconds=3)
    message = (await store.list_pending_oldest())[0]
    assert message.image_error and "10 images" in message.image_error
    claim = await store.claim_inbox(message.id, owner="worker")
    await store.finish_inbox(claim, status="processed")
    await store.prune_inbox([item.id for item in await store.list_inbox()])
    assert not any((tmp_path / "user" / settings.messaging.attachment_dir).rglob("*.png"))


async def test_failed_inbox_transaction_removes_only_its_new_snapshots(tmp_path: Path) -> None:
    from ricky.messaging.store import MessagingStoreError

    settings = _settings(tmp_path)
    clock = MutableClock()
    store = MessagingStore(settings, clock=clock)
    await store.initialize()
    first = image_update(clock, "1", album=None)
    second = image_update(clock, "2", album=None)
    second = second.model_copy(
        update={"message": second.message.model_copy(update={"id": first.message.id})}
    )
    with pytest.raises(MessagingStoreError):
        await store.ingest(_batch(clock, "1", "2").model_copy(update={"updates": [first, second]}))
    assert await store.list_inbox() == []
    assert not any((tmp_path / "user" / settings.messaging.attachment_dir).rglob("*.png"))
