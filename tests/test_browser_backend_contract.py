"""Private browser backend contract and deterministic fake tests."""

from __future__ import annotations

import asyncio

import pytest

from browser_support import FakeBrowserPage, FakeBrowserSession
from ricky.browser.backend import (
    BackendActionOutcome,
    BackendActionPreflight,
    BackendActionRequest,
    BackendCoordinatePreflight,
    BackendCoordinateRequest,
    BackendPageState,
    BackendTargetDescriptor,
    BackendViewport,
)
from ricky.browser.types import BrowserActionRequest, BrowserError

ACTION_ID = "browser_action_" + "d" * 32


def test_backend_target_projects_only_provider_safe_descriptor_facts() -> None:
    target = BackendTargetDescriptor(
        ref="e7",
        role="textbox",
        name="Email",
        control_kind="email",
        frame_origin="https://example.com",
        frame_key="private-frame-identity",
        editable=True,
    )

    projected = target.provider_descriptor()

    assert projected.ref == "e7"
    assert projected.frame_origin == "https://example.com"
    assert "private-frame-identity" not in projected.model_dump_json()
    assert "frame_key" not in type(projected).model_fields


async def test_fake_backend_preflights_then_dispatches_exactly_once() -> None:
    target = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Continue",
        control_kind="button",
    )
    page = FakeBrowserPage(
        snapshot='- button "Continue" [ref=e1]',
        targets=(target,),
    )
    request = BackendActionRequest(
        action_id=ACTION_ID,
        action=BrowserActionRequest(kind="click"),
        target=target,
    )

    resolved = await page.preflight_action(request)
    outcome = await page.perform_action(request)

    assert resolved == BackendActionPreflight(target=target)
    assert outcome.disposition == "performed"
    assert outcome.dispatch_state == "completed"
    assert page.preflights == [request]
    assert page.actions == [request]
    assert page.operations == ["preflight", "action:start", "action:end"]


async def test_fake_backend_binds_semantic_dispatch_to_preflight_destinations() -> None:
    target = BackendTargetDescriptor(ref="e1", role="button", name="Submit")
    page = FakeBrowserPage(targets=(target,))
    page.effective_destinations = ("https://example.com/first",)
    initial = BackendActionRequest(
        action_id=ACTION_ID,
        action=BrowserActionRequest(kind="commit", activation="click"),
        target=target,
    )
    preflight = await page.preflight_action(initial)

    page.effective_destinations = ("https://example.com/changed",)
    outcome = await page.perform_action(
        BackendActionRequest(
            action_id=ACTION_ID,
            action=initial.action,
            target=target,
            expected_preflight=preflight,
        )
    )

    assert outcome.disposition == "not_performed"
    assert outcome.failure is not None
    assert outcome.failure.code == "stale_target"
    assert page.actions == []


async def test_fake_backend_coordinate_preflight_binds_nested_target_and_destinations() -> None:
    page = FakeBrowserPage()
    page.coordinate_target = BackendTargetDescriptor(
        ref="d0",
        role="button",
        name="Donate",
        frame_key="frame-1",
        frame_origin="https://pay.example",
        consequential=True,
    )
    page.coordinate_destinations = ("https://pay.example/confirm",)
    page.coordinate_financial_signal = True
    request = BackendCoordinateRequest(
        action_id=ACTION_ID,
        x=10,
        y=20,
        masked_base_sha256="a" * 64,
        viewport=BackendViewport(
            width=100,
            height=100,
            scroll_x=0,
            scroll_y=0,
            device_scale_factor=1,
        ),
    )

    preflight = await page.preflight_coordinate_commit(request)

    assert preflight == BackendCoordinatePreflight(
        target=page.coordinate_target,
        effective_destinations=page.coordinate_destinations,
        financial_signal=True,
    )
    page.coordinate_target = BackendTargetDescriptor(ref="d0", role="link", name="Changed")
    outcome = await page.perform_coordinate_commit(request, expected=preflight)
    assert outcome.disposition == "not_performed"
    assert outcome.failure is not None
    assert outcome.failure.code == "stale_target"
    assert page.coordinates == []


async def test_fake_backend_rejects_missing_or_changed_target_before_dispatch() -> None:
    current = BackendTargetDescriptor(ref="e1", role="button", name="Current")
    page = FakeBrowserPage(targets=(current,))

    with pytest.raises(BrowserError, match="unknown browser target"):
        await page.preflight_action(
            BackendActionRequest(
                action_id=ACTION_ID,
                action=BrowserActionRequest(kind="click"),
                target=BackendTargetDescriptor(ref="e2"),
            )
        )
    with pytest.raises(BrowserError, match="target facts changed"):
        await page.preflight_action(
            BackendActionRequest(
                action_id=ACTION_ID,
                action=BrowserActionRequest(kind="click"),
                target=BackendTargetDescriptor(ref="e1", role="button", name="Stale"),
            )
        )

    assert page.actions == []


async def test_fake_backend_preserves_duplicate_refs_for_ambiguous_target_tests() -> None:
    first = BackendTargetDescriptor(ref="e1", role="button", name="First")
    second = BackendTargetDescriptor(ref="e1", role="button", name="Second")
    page = FakeBrowserPage(targets=(first, second))

    snapshot = await page.snapshot(depth=20, character_limit=20_000)
    with pytest.raises(BrowserError, match="ambiguous browser target"):
        await page.preflight_action(
            BackendActionRequest(
                action_id=ACTION_ID,
                action=BrowserActionRequest(kind="click"),
                target=first,
            )
        )

    assert snapshot.targets == (first, second)
    assert page.actions == []


async def test_fake_backend_action_wait_is_cancellable_without_implicit_replay() -> None:
    target = BackendTargetDescriptor(ref="e1", role="button", name="Wait")
    page = FakeBrowserPage(targets=(target,))
    page.action_entered = asyncio.Event()
    page.action_release = asyncio.Event()
    request = BackendActionRequest(
        action_id=ACTION_ID,
        action=BrowserActionRequest(kind="click"),
        target=target,
    )

    task = asyncio.create_task(page.perform_action(request))
    await page.action_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert page.actions == [request]
    assert page.operations == ["action:start"]


async def test_fake_backend_can_surface_popup_and_closed_page_outcome() -> None:
    target = BackendTargetDescriptor(ref="e1", role="button", name="Open")
    page = FakeBrowserPage(targets=(target,))
    session = FakeBrowserSession([page])
    page.action_popups = [FakeBrowserPage(key="popup", url="https://example.com/popup")]
    state_before = await page.state()
    page.action_outcomes.append(
        BackendActionOutcome(
            disposition="in_doubt",
            dispatch_state="dispatched",
            state_before=state_before,
            state_after=None,
            failure=None,
        )
    )

    outcome = await page.perform_action(
        BackendActionRequest(
            action_id=ACTION_ID,
            action=BrowserActionRequest(kind="click"),
            target=target,
        )
    )

    assert outcome.state_after is None
    assert [handle.key for handle in await session.pages()] == ["page-1", "popup"]


def test_backend_outcome_can_preserve_last_known_state_after_page_loss() -> None:
    state = BackendPageState(key="page-1", url="https://example.com/", title="Example")

    outcome = BackendActionOutcome(
        disposition="in_doubt",
        dispatch_state="dispatched",
        state_before=state,
    )

    assert outcome.state_before == state
    assert outcome.state_after is None


def test_backend_request_rejects_non_opaque_action_id() -> None:
    with pytest.raises(ValueError, match="action id must be opaque"):
        BackendActionRequest(
            action_id="retry-this-action",
            action=BrowserActionRequest(kind="click"),
            target=BackendTargetDescriptor(ref="e1"),
        )


def test_backend_outcome_rejects_inconsistent_dispatch_evidence() -> None:
    state = BackendPageState(key="page-1", url="https://example.com/", title="Example")

    with pytest.raises(ValueError, match="inconsistent"):
        BackendActionOutcome(
            disposition="performed",
            dispatch_state="not_dispatched",
            state_before=state,
        )
