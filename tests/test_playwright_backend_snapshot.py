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
