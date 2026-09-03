"""Local deterministic composition for masked browser viewport screenshots."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from ricky.browser.backend import BackendVisualSnapshot
from ricky.browser.types import BrowserError, BrowserFailure
from ricky.config import BrowserSettings


@dataclass(frozen=True)
class ComposedVisual:
    """Provider artifact bytes and their scale from CSS viewport coordinates."""

    png: bytes
    width: int
    height: int
    image_scale: float


def compose_numbered_visual(
    capture: BackendVisualSnapshot,
    settings: BrowserSettings,
) -> ComposedVisual:
    """Decode one owned masked PNG, resize within policy, and add numbered labels."""
    if len(capture.png) > settings.screenshot_file_byte_limit:
        raise _too_large("masked screenshot exceeds the configured byte limit")
    try:
        with Image.open(io.BytesIO(capture.png)) as source:
            if source.format != "PNG":
                raise ValueError("browser visual capture is not PNG")
            width, height = source.size
            if width < 1 or height < 1 or width * height > settings.screenshot_pixel_limit:
                raise _too_large("masked screenshot exceeds the configured pixel limit")
            image = source.convert("RGBA")
    except BrowserError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise BrowserError(
            BrowserFailure(
                code="visual_capture_failed",
                message="browser visual capture could not be decoded safely",
            )
        ) from exc

    factor = min(
        1.0,
        settings.screenshot_width_limit / width,
        settings.screenshot_height_limit / height,
        math.sqrt(settings.screenshot_pixel_limit / (width * height)),
    )
    if factor < 1.0:
        resized = (
            max(1, math.floor(width * factor)),
            max(1, math.floor(height * factor)),
        )
        image = image.resize(resized, resample=Image.Resampling.LANCZOS)
    output_width, output_height = image.size
    image_scale = output_width / width

    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for number, candidate in enumerate(capture.candidates, start=1):
        box = candidate.bounding_box
        label = str(number)
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font, stroke_width=1)
        label_width = right - left + 6
        label_height = bottom - top + 4
        x = min(max(0, round(box.x * image_scale)), max(0, output_width - label_width))
        y = min(max(0, round(box.y * image_scale)), max(0, output_height - label_height))
        draw.rectangle(
            (x, y, x + label_width, y + label_height),
            fill=(15, 15, 15, 255),
            outline=(255, 255, 255, 255),
            width=1,
        )
        draw.text(
            (x + 3, y + 2),
            label,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=1,
            stroke_fill=(0, 0, 0, 255),
        )

    encoded = io.BytesIO()
    image.convert("RGB").save(encoded, format="PNG", optimize=False, compress_level=9)
    png = encoded.getvalue()
    if len(png) > settings.screenshot_file_byte_limit:
        raise _too_large("composed screenshot exceeds the configured byte limit")
    return ComposedVisual(
        png=png,
        width=output_width,
        height=output_height,
        image_scale=image_scale,
    )


def _too_large(message: str) -> BrowserError:
    return BrowserError(BrowserFailure(code="screenshot_too_large", message=message))
