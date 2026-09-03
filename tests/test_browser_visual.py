"""Deterministic local browser screenshot composition tests."""

from __future__ import annotations

import hashlib
from io import BytesIO

import pytest
from PIL import Image

from ricky.browser.backend import (
    BackendBoundingBox,
    BackendTargetDescriptor,
    BackendViewport,
    BackendVisualCandidate,
    BackendVisualSnapshot,
)
from ricky.browser.types import BrowserError
from ricky.browser.visual import compose_numbered_visual
from ricky.config import BrowserSettings


def _png(width: int = 100, height: int = 50) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), (240, 240, 240)).save(output, format="PNG")
    return output.getvalue()


def _capture(content: bytes) -> BackendVisualSnapshot:
    return BackendVisualSnapshot(
        png=content,
        masked_base_sha256=hashlib.sha256(content).hexdigest(),
        viewport=BackendViewport(
            width=100,
            height=50,
            scroll_x=0,
            scroll_y=0,
            device_scale_factor=1,
        ),
        candidates=(
            BackendVisualCandidate(
                descriptor=BackendTargetDescriptor(ref="d1", role="button", name="One"),
                bounding_box=BackendBoundingBox(x=0, y=0, width=20, height=10),
            ),
            BackendVisualCandidate(
                descriptor=BackendTargetDescriptor(ref="d2", role="button", name="Two"),
                bounding_box=BackendBoundingBox(x=99, y=49, width=1, height=1),
            ),
        ),
    )


def test_visual_composition_is_deterministic_clamped_and_uniformly_resized() -> None:
    content = _png()
    capture = _capture(content)
    settings = BrowserSettings(
        screenshot_width_limit=50,
        screenshot_height_limit=50,
        screenshot_pixel_limit=5_000,
        screenshot_file_byte_limit=50_000,
    )

    first = compose_numbered_visual(capture, settings)
    second = compose_numbered_visual(capture, settings)

    assert first == second
    assert (first.width, first.height) == (50, 25)
    assert first.image_scale == 0.5
    assert capture.png == content
    assert capture.masked_base_sha256 == hashlib.sha256(content).hexdigest()
    with Image.open(BytesIO(first.png)) as composed:
        assert composed.format == "PNG"
        assert composed.mode == "RGB"
        assert composed.size == (50, 25)
        assert composed.getpixel((0, 0)) != (240, 240, 240)
        assert composed.getpixel((49, 24)) != (240, 240, 240)


@pytest.mark.parametrize(
    ("capture", "match"),
    [
        (
            BackendVisualSnapshot(
                png=b"not a png",
                masked_base_sha256="0" * 64,
                viewport=BackendViewport(100, 50, 0, 0, 1),
                candidates=(),
            ),
            "decoded safely",
        ),
        (_capture(_png(101, 50)), "pixel limit"),
    ],
)
def test_visual_composition_rejects_corrupt_or_oversized_input(
    capture: BackendVisualSnapshot,
    match: str,
) -> None:
    settings = BrowserSettings(
        screenshot_width_limit=100,
        screenshot_height_limit=100,
        screenshot_pixel_limit=5_000,
        screenshot_file_byte_limit=50_000,
    )

    with pytest.raises(BrowserError, match=match):
        compose_numbered_visual(capture, settings)


def test_visual_composition_enforces_post_composition_byte_ceiling() -> None:
    content = _png(10, 10)
    settings = BrowserSettings(
        screenshot_width_limit=10,
        screenshot_height_limit=10,
        screenshot_pixel_limit=100,
        screenshot_file_byte_limit=len(content),
    )
    capture = BackendVisualSnapshot(
        png=content,
        masked_base_sha256=hashlib.sha256(content).hexdigest(),
        viewport=BackendViewport(10, 10, 0, 0, 1),
        candidates=(
            BackendVisualCandidate(
                descriptor=BackendTargetDescriptor(ref="d1"),
                bounding_box=BackendBoundingBox(0, 0, 10, 10),
            ),
        ),
    )

    with pytest.raises(BrowserError, match="composed screenshot"):
        compose_numbered_visual(capture, settings)
