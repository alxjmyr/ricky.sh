"""Focused bounds tests for Playwright semantic snapshot enrichment."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from ricky.browser.playwright_backend import _PlaywrightPage
from ricky.browser.types import BrowserError


class _SnapshotPage:
    def __init__(self, content: str) -> None:
        self.content = content
        self.snapshot_calls: list[tuple[int, str]] = []

    def is_closed(self) -> bool:
        return False

    async def aria_snapshot(self, *, depth: int, mode: str) -> str:
        self.snapshot_calls.append((depth, mode))
        return self.content


class _SnapshotSession:
    def __init__(self, operation_timeout_seconds: float = 1.0) -> None:
        self._operation_timeout_seconds = operation_timeout_seconds

    def ensure_connected(self) -> None:
        return None

    def is_quarantined(self, _page: object) -> bool:
        return False


def _page(
    content: str,
    *,
    operation_timeout_seconds: float = 1.0,
) -> tuple[_PlaywrightPage, _SnapshotPage]:
    raw_page = _SnapshotPage(content)
    page = _PlaywrightPage(
        cast(Any, raw_page),
        cast(Any, _SnapshotSession(operation_timeout_seconds)),
    )
    return page, raw_page


async def test_snapshot_enriches_only_complete_lines_inside_character_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = [f'- button "Control {index}" [ref=e{index}]\n' for index in range(5_000)]
    page, raw_page = _page("".join(lines))
    resolved_refs: list[str] = []

    async def resolve_target(
        ref: str,
        *,
        allow_ambiguous: bool = False,
    ) -> None:
        assert allow_ambiguous is True
        resolved_refs.append(ref)
        return None

    monkeypatch.setattr(page, "_resolve_target", resolve_target)
    limit = sum(len(line) for line in lines[:3]) + 5

    snapshot = await page.snapshot(depth=7, character_limit=limit)

    assert raw_page.snapshot_calls == [(7, "ai")]
    assert snapshot.content == "".join(lines[:3])
    assert snapshot.character_truncated is True
    assert [target.ref for target in snapshot.targets] == ["e0", "e1", "e2"]
    assert resolved_refs == ["e0", "e1", "e2"]
    assert len(snapshot.content) <= limit


async def test_snapshot_uses_one_deadline_for_all_target_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, _ = _page(
        '- button "Slow" [ref=e1]\n',
        operation_timeout_seconds=0.001,
    )

    async def never_finishes(_: str) -> tuple[()]:
        await asyncio.Event().wait()
        return ()

    monkeypatch.setattr(page, "_snapshot_targets", never_finishes)

    with pytest.raises(BrowserError) as exc_info:
        await page.snapshot(depth=3, character_limit=1_000)

    assert exc_info.value.failure.code == "operation_timeout"
    assert exc_info.value.failure.retryable is True


async def test_snapshot_preserves_untruncated_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = '- button "Ready" [ref=e1]'
    page, _ = _page(content)

    async def no_targets(_: str) -> tuple[()]:
        return ()

    monkeypatch.setattr(page, "_snapshot_targets", no_targets)

    snapshot = await page.snapshot(depth=2, character_limit=len(content))

    assert snapshot.content == content
    assert snapshot.character_truncated is False


async def test_snapshot_keeps_refs_for_nonstandard_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page, _ = _page(
        '- heading "Purchase credits" [ref=e1]\n'
        '- paragraph "Saved payment" [ref=e2]\n'
        '- generic "Custom control" [ref=e3] [cursor=pointer]\n'
        '- spinbutton "Credit amount" [ref=e4]\n'
    )
    resolved = []

    async def resolve(ref: str, *, allow_ambiguous: bool = False) -> None:
        resolved.append(ref)
        return None

    monkeypatch.setattr(page, "_resolve_target", resolve)
    snapshot = await page.snapshot(depth=7, character_limit=4_000)
    assert resolved == ["e1", "e2", "e3", "e4"]
    assert len(snapshot.targets) == 4
    assert not snapshot.targets[0].consequential


@pytest.mark.parametrize("modal", [True, False])
async def test_only_verified_modal_omits_background(
    monkeypatch: pytest.MonkeyPatch, modal: bool
) -> None:
    content = (
        '- heading "Background" [ref=e1]\n'
        '- dialog "Checkout" [ref=e2]:\n'
        '  - button "Pay" [ref=e3]\n'
        '- button "Background action" [ref=e4]\n'
    )
    page, _ = _page(content)

    class Locator:
        async def evaluate(self, expression: str) -> bool:
            assert "aria-modal" in expression
            return modal

    async def resolve(ref: str, *, allow_ambiguous: bool = False):
        return None, Locator()

    monkeypatch.setattr(page, "_resolve_target", resolve)
    result = await page._focus_modal_snapshot(content)
    assert ("Background" not in result) == modal
    assert 'button "Pay"' in result


@pytest.mark.parametrize(
    ("label", "autocomplete", "input_type", "protected"),
    [
        ("Credit amount", "transaction-amount", "number", False),
        ("Debit amount", "transaction-amount", "number", False),
        ("Currency", "transaction-currency", "text", False),
        ("Use one-time payment method", "", "checkbox", False),
        ("Credit card number", "", "text", True),
        ("Card security code", "cc-csc", "text", True),
        ("One-time code", "", "text", True),
        ("Verification", "one-time-code", "text", True),
        ("Sign in", "", "password", True),
    ],
)
def test_amount_fields_are_not_credentials(
    label: str, autocomplete: str, input_type: str, protected: bool
) -> None:
    from ricky.browser.playwright_backend import _coordinate_target_descriptor

    target = _coordinate_target_descriptor(
        {"tag": "input", "aria": label, "autocomplete": autocomplete, "type": input_type},
    )
    assert target.protected is protected
    assert (target.protected_kind is not None) is protected


@pytest.mark.parametrize("outcome", ["success", "cancel", "error"])
async def test_visual_inspection_is_bounded_ordered_and_joined(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from types import SimpleNamespace

    from playwright.async_api import Error as PlaywrightError

    from ricky.browser.backend import (
        BackendBoundingBox,
        BackendTargetDescriptor,
        BackendViewport,
        BackendVisualCandidate,
    )

    page, raw_page = _page("", operation_timeout_seconds=5)

    async def indices(*_: Any) -> list[int]:
        return list(range(30))

    locator = SimpleNamespace(evaluate_all=indices, nth=lambda index: index)
    frame = SimpleNamespace(locator=lambda _: locator)
    monkeypatch.setattr(raw_page, "frames", [frame], raising=False)
    monkeypatch.setattr(raw_page, "main_frame", frame, raising=False)
    viewport = BackendViewport(width=800, height=600, scroll_x=0, scroll_y=0, device_scale_factor=1)

    async def capture() -> tuple[BackendViewport, bytes]:
        return viewport, b"same masked image"

    monkeypatch.setattr(page, "_masked_viewport_png", capture)
    gates = [asyncio.Event() for _ in range(30)]
    started: list[int] = []
    finished: list[int] = []
    batch_ready = asyncio.Event()

    async def inspect(index: int, **_: Any) -> BackendVisualCandidate:
        started.append(index)
        if len(started) == 8:
            batch_ready.set()
        try:
            await gates[index].wait()
            if outcome == "error":
                raise PlaywrightError("fixture inspection failure")
            return BackendVisualCandidate(
                descriptor=BackendTargetDescriptor(ref="pending", name=str(index)),
                bounding_box=BackendBoundingBox(x=0, y=0, width=20, height=20),
            )
        finally:
            finished.append(index)

    monkeypatch.setattr(page, "_visual_candidate", inspect)
    task = asyncio.create_task(page.visual_snapshot(candidate_limit=10))
    try:
        await asyncio.wait_for(batch_ready.wait(), 1)
        assert started == list(range(8))
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "error":
            gates[3].set()
            with pytest.raises(BrowserError, match="visual browser snapshot failed"):
                await task
        else:
            # Complete later controls first; output must still follow DOM order.
            for index in range(29, -1, -1):
                gates[index].set()
                await asyncio.sleep(0)
            result = await task
            assert [item.descriptor.name for item in result.candidates] == [
                str(i) for i in range(10)
            ]
            assert [item.descriptor.ref for item in result.candidates] == [
                f"d{i}" for i in range(1, 11)
            ]
            assert result.candidate_truncated
            assert started == list(range(11))
        assert set(finished) == set(started)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["missing", "success", "error", "cancel"])
async def test_visual_candidate_pins_and_disposes_one_element(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from types import SimpleNamespace

    from ricky.browser.backend import BackendViewport

    page, _ = _page("")
    disposed = asyncio.Event()
    entered = asyncio.Event()

    async def dispose() -> None:
        disposed.set()

    handle = SimpleNamespace(dispose=dispose)

    async def resolve() -> list[Any]:
        return [] if outcome == "missing" else [handle]

    async def inspect(element: Any, **_: Any) -> None:
        assert element is handle
        entered.set()
        if outcome == "error":
            raise RuntimeError("fixture inspection failed")
        if outcome == "cancel":
            await asyncio.Event().wait()
        return None

    monkeypatch.setattr(page, "_visual_element", inspect)
    viewport = BackendViewport(width=100, height=100, scroll_x=0, scroll_y=0, device_scale_factor=1)
    task = asyncio.create_task(
        page._visual_candidate(
            cast(Any, SimpleNamespace(element_handles=resolve)),
            frame=cast(Any, object()),
            viewport=viewport,
        )
    )
    try:
        if outcome == "cancel":
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "error":
            with pytest.raises(RuntimeError, match="fixture inspection failed"):
                await task
        else:
            assert await task is None
        assert disposed.is_set() == (outcome != "missing")
        assert entered.is_set() == (outcome != "missing")
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("detached,second_fails", [(True, False), (True, True), (False, False)])
async def test_masked_capture_retries_only_detachment_with_fresh_masks(
    monkeypatch: pytest.MonkeyPatch, detached: bool, second_fails: bool
) -> None:
    from types import SimpleNamespace

    from playwright.async_api import Error as PlaywrightError

    page, raw_page = _page("")
    old = SimpleNamespace(locator=lambda _: "old-mask", is_detached=lambda: detached)
    new = SimpleNamespace(locator=lambda _: "new-mask", is_detached=lambda: False)
    monkeypatch.setattr(raw_page, "frames", [old], raising=False)
    reads = 0
    calls: list[dict[str, Any]] = []

    async def metrics(_: str) -> dict[str, int]:
        nonlocal reads
        reads += 1
        return {"width": 100, "height": 100, "scrollX": reads, "scrollY": 0, "deviceScaleFactor": 1}

    async def screenshot(**kwargs: Any) -> bytes:
        calls.append(kwargs)
        if len(calls) == 1:
            monkeypatch.setattr(raw_page, "frames", [new])
            raise PlaywrightError("fixture capture failed")
        if second_fails:
            raise PlaywrightError("second capture failed")
        return b"masked pixels"

    monkeypatch.setattr(raw_page, "evaluate", metrics, raising=False)
    monkeypatch.setattr(raw_page, "screenshot", screenshot, raising=False)
    if detached and not second_fails:
        viewport, png = await page._masked_viewport_png()
        assert png == b"masked pixels"
        assert viewport.scroll_x == 2
    else:
        with pytest.raises(PlaywrightError):
            await page._masked_viewport_png()
    assert len(calls) == (2 if detached else 1)
    assert calls[0]["mask"] == ["old-mask"]
    if detached:
        assert calls[1]["mask"] == ["new-mask"]
    assert all(call["mask_color"] == "#4b0082" for call in calls)


@pytest.mark.parametrize("same_element", [True, False])
async def test_visual_candidate_rejects_reordered_action_locator(
    monkeypatch: pytest.MonkeyPatch, same_element: bool
) -> None:
    from types import SimpleNamespace

    from ricky.browser.backend import (
        BackendBoundingBox,
        BackendTargetDescriptor,
        BackendViewport,
        BackendVisualCandidate,
    )

    page, _ = _page("")
    disposed: list[bool] = []

    async def dispose() -> None:
        disposed.append(True)

    handle = SimpleNamespace(dispose=dispose)

    async def resolve() -> list[Any]:
        return [handle]

    async def same(_: str, expected: object) -> bool:
        assert expected is handle
        return same_element

    result = BackendVisualCandidate(
        descriptor=BackendTargetDescriptor(ref="pending", name="Original control"),
        bounding_box=BackendBoundingBox(x=0, y=0, width=20, height=20),
    )

    async def inspect(element: object, **_: Any) -> BackendVisualCandidate:
        assert element is handle
        return result

    monkeypatch.setattr(page, "_visual_element", inspect)
    viewport = BackendViewport(width=100, height=100, scroll_x=0, scroll_y=0, device_scale_factor=1)
    locator = cast(Any, SimpleNamespace(element_handles=resolve, evaluate_all=same))
    if same_element:
        assert (
            await page._visual_candidate(locator, frame=cast(Any, object()), viewport=viewport)
            is result
        )
    else:
        with pytest.raises(BrowserError) as error:
            await page._visual_candidate(locator, frame=cast(Any, object()), viewport=viewport)
        assert error.value.failure.code == "stale_target"
    assert disposed == [True]
