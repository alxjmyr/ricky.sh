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
