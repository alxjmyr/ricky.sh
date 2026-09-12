"""Validate and snapshot explicitly selected static image inputs."""

from __future__ import annotations

import math
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from ricky.config import RickySettings
from ricky.media.store import SessionMediaError, SessionMediaLimitError


class ImageUpload(BaseModel):
    """Immutable normalized image; bytes round-trip as base64 in private boundaries."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    filename: str = Field(min_length=1, max_length=255)
    content: bytes = Field(min_length=1, repr=False)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    resized: bool = False


def restore_image_upload(filename: str, content: bytes) -> ImageUpload:
    """Reopen a digest-checked normalized snapshot without reapplying source limits."""
    try:
        with Image.open(BytesIO(content)) as source:
            if source.format != "PNG" or getattr(source, "n_frames", 1) != 1:
                raise SessionMediaError("stored image is not a normalized static PNG")
            width, height = source.size
            source.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise SessionMediaError("stored image is unreadable or corrupt") from exc
    return ImageUpload(filename=filename, content=content, width=width, height=height)


def normalize_image_upload(filename: str, content: bytes, settings: RickySettings) -> ImageUpload:
    """Decode a bounded static image, orient it, strip metadata and encode PNG."""
    limits = settings.context.media
    # Names are labels, never storage paths or terminal control sequences.
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable())[:255] or "image"
    if not content or len(content) > limits.upload_image_byte_limit:
        raise SessionMediaLimitError(f"{name}: image exceeds the upload byte limit")
    try:
        with Image.open(BytesIO(content)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"}:
                raise SessionMediaError(f"{name}: use a static PNG, JPEG, or WebP image")
            if getattr(source, "n_frames", 1) != 1:
                raise SessionMediaError(f"{name}: animated images are not supported")
            if source.width * source.height > limits.upload_image_pixel_limit:
                raise SessionMediaLimitError(f"{name}: image exceeds the upload pixel limit")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            mode = "RGBA" if "A" in oriented.getbands() or "transparency" in source.info else "RGB"
            converted = oriented.convert(mode)
            clean = Image.frombytes(mode, converted.size, converted.tobytes())
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise SessionMediaError(f"{name}: image is unreadable or corrupt") from exc

    original_size = clean.size
    pixel_limit = limits.request_image_pixel_limit
    if clean.width * clean.height > pixel_limit:
        scale = math.sqrt(pixel_limit / (clean.width * clean.height))
        clean = clean.resize(
            (max(1, int(clean.width * scale)), max(1, int(clean.height * scale))),
            Image.Resampling.LANCZOS,
        )
    while True:
        output = BytesIO()
        clean.save(output, format="PNG")
        encoded = output.getvalue()
        if len(encoded) <= limits.request_image_byte_limit:
            break
        if clean.size == (1, 1):
            raise SessionMediaLimitError(f"{name}: image cannot fit the request byte limit")
        clean = clean.resize(
            (max(1, int(clean.width * 0.8)), max(1, int(clean.height * 0.8))),
            Image.Resampling.LANCZOS,
        )
    return ImageUpload(
        filename=name,
        content=encoded,
        width=clean.width,
        height=clean.height,
        resized=clean.size != original_size,
    )
