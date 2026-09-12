"""Static image decoding and complete-set admission boundaries."""

from __future__ import annotations

import asyncio
import threading
from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.media import (
    ImageUpload,
    SessionMediaError,
    SessionMediaLimitError,
    SessionMediaStore,
    normalize_image_upload,
    restore_image_upload,
)


def picture(format="PNG", size=(8, 6), **kwargs):
    output = BytesIO()
    Image.new("RGB", size, (12, 34, 56)).save(output, format=format, **kwargs)
    return output.getvalue()


def settings_at(tmp_path, **limits):
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "context": {"media": limits},
        }
    )


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_standard_static_images_normalize_and_round_trip(tmp_path, format):
    upload = normalize_image_upload("/some/path/image", picture(format), settings_at(tmp_path))
    assert upload.filename == "image"
    assert ImageUpload.model_validate_json(upload.model_dump_json()) == upload
    with Image.open(BytesIO(upload.content)) as image:
        assert image.format == "PNG"
        assert image.size == (8, 6)
        assert not image.info


def test_orientation_and_metadata_are_normalized(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private description"
    upload = normalize_image_upload(
        "rotated.jpg", picture("JPEG", exif=exif), settings_at(tmp_path)
    )
    assert (upload.width, upload.height) == (6, 8)
    with Image.open(BytesIO(upload.content)) as image:
        assert not image.getexif()
        assert not image.info
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "private note")
    clean = normalize_image_upload("image.png", picture(pnginfo=metadata), settings_at(tmp_path))
    assert b"private note" not in clean.content


def test_invalid_animated_and_oversized_inputs_are_rejected(tmp_path):
    settings = settings_at(tmp_path)
    for content in (b"not an image", picture("GIF"), picture()[:35]):
        with pytest.raises(SessionMediaError):
            normalize_image_upload("bad", content, settings)
    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        output,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
    )
    with pytest.raises(SessionMediaError, match="animated"):
        normalize_image_upload("animation.png", output.getvalue(), settings)
    with pytest.raises(SessionMediaLimitError, match="upload byte"):
        normalize_image_upload(
            "large.png", picture(), settings_at(tmp_path, upload_image_byte_limit=2)
        )
    with pytest.raises(SessionMediaLimitError, match="upload pixel"):
        normalize_image_upload(
            "large.png", picture(), settings_at(tmp_path, upload_image_pixel_limit=2)
        )


def test_resizing_is_reported_and_snapshot_is_immutable(tmp_path):
    upload = normalize_image_upload(
        "image.png", picture(), settings_at(tmp_path, request_image_pixel_limit=12)
    )
    assert upload.resized
    assert upload.width * upload.height <= 12
    with pytest.raises(ValueError):
        upload.width = 9


def test_normalized_snapshot_restoration_does_not_reapply_source_limits(tmp_path):
    normalized = normalize_image_upload("input.jpg", picture("JPEG"), settings_at(tmp_path))
    restored = restore_image_upload(normalized.filename, normalized.content)
    assert restored == normalized
    with pytest.raises(SessionMediaError):
        restore_image_upload("fake.png", picture("JPEG"))


@pytest.mark.parametrize(
    "limits",
    [
        {"request_image_pixel_limit": 60},
        {"request_image_byte_limit": 100},
        {"session_byte_limit": 100},
    ],
)
async def test_batch_aggregate_ceiling_rejects_without_writing(tmp_path, limits):
    settings = settings_at(tmp_path, **limits)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionMediaStore.create(settings, session.id)
    upload = normalize_image_upload("image.png", picture(), settings)
    with pytest.raises(SessionMediaLimitError):
        await store.admit_images(session, images=[upload, upload])
    assert session.media == []
    assert not store.root.exists()


async def test_complete_batch_retains_profile_and_reopens_in_user_root(tmp_path):
    settings = settings_at(tmp_path, request_image_limit=20)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionMediaStore.create(settings, session.id)
    uploads = [normalize_image_upload(f"{i}.jpg", picture("JPEG"), settings) for i in range(10)]
    records = await store.admit_images(session, images=uploads, retention="conversation")
    assert len(session.media) == 10
    assert all(record.provenance == "user_upload" for record in records)
    assert all(
        record.source_label.required_profiles == (session.profile_scope.primary,)
        for record in records
    )
    reopened = SessionMediaStore.create(settings, session.id)
    restored = AgentSession.model_validate_json(session.model_dump_json())
    resolver = reopened.resolver(
        restored, provider=session.provider, profile_scope=session.profile_scope
    )
    assert (await resolver.resolve(records[0].reference())).content == uploads[0].content
    assert not (tmp_path / "project").exists()
    await store.remove_records(session, {record.id for record in records})
    assert session.media == []
    assert not store.root.exists()


async def test_batch_limits_and_write_failure_never_leave_partial_media(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, request_image_limit=20)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionMediaStore.create(settings, session.id)
    upload = normalize_image_upload("image.png", picture(), settings)
    with pytest.raises(SessionMediaLimitError):
        await store.admit_images(session, images=[upload] * 11)
    original = store._write_atomic
    count = 0

    def fail_second(path, content):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("disk full")
        original(path, content)

    monkeypatch.setattr(store, "_write_atomic", fail_second)
    with pytest.raises(OSError, match="disk full"):
        await store.admit_images(session, images=[upload, upload])
    assert session.media == []
    assert not store.root.exists()


async def test_cancelled_batch_joins_writer_and_removes_all_files(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, request_image_limit=20)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionMediaStore.create(settings, session.id)
    upload = normalize_image_upload("image.png", picture(), settings)
    started = threading.Event()
    release = threading.Event()
    original = store._write_atomic

    def pause(path, content):
        original(path, content)
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(store, "_write_atomic", pause)
    task = asyncio.create_task(store.admit_images(session, images=[upload, upload]))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.media == []
    assert not store.root.exists()
