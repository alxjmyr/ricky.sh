"""Browser action coordination, fresh-state, and handoff tests."""

from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from browser_support import FakeBrowserBackend, FakeBrowserPage, fake_executable
from ricky.attachments import LoadedAttachment, browser_download_path
from ricky.browser.backend import (
    BackendActionOutcome,
    BackendBoundingBox,
    BackendDownload,
    BackendDownloadOutcome,
    BackendTargetDescriptor,
    BackendViewport,
    BackendVisualCandidate,
    BackendVisualSnapshot,
)
from ricky.browser.service import BrowserService
from ricky.browser.types import (
    BrowserActionRequest,
    BrowserActionTarget,
    BrowserCoordinateTarget,
    BrowserDialogPolicy,
    BrowserError,
    BrowserFailure,
)
from ricky.config import RickySettings

_ORIGIN = "http://127.0.0.1:8765"


def _service(
    tmp_path: Path,
    *,
    headless: bool = True,
    max_pages: int = 8,
    download_file_byte_limit: int = 50_000_000,
) -> tuple[BrowserService, FakeBrowserBackend]:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "browser": {
                "enabled": True,
                "headless": headless,
                "max_pages": max_pages,
                "download_file_byte_limit": download_file_byte_limit,
                "allowed_private_origins": [_ORIGIN],
            },
        }
    )
    backend = FakeBrowserBackend()
    tmp_path.mkdir(parents=True, exist_ok=True)
    return (
        BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=backend,
            executable_path=fake_executable(tmp_path),
        ),
        backend,
    )


def _visual_capture(
    descriptor: BackendTargetDescriptor,
    *,
    color: tuple[int, int, int] = (230, 230, 230),
) -> BackendVisualSnapshot:
    output = BytesIO()
    Image.new("RGB", (100, 50), color).save(output, format="PNG")
    png = output.getvalue()
    return BackendVisualSnapshot(
        png=png,
        masked_base_sha256=hashlib.sha256(png).hexdigest(),
        viewport=BackendViewport(
            width=100,
            height=50,
            scroll_x=2,
            scroll_y=3,
            device_scale_factor=1,
        ),
        candidates=(
            BackendVisualCandidate(
                descriptor=descriptor,
                bounding_box=BackendBoundingBox(x=10, y=5, width=20, height=10),
            ),
        ),
    )


def _action_target(
    snapshot_id: str,
    session_id: str,
    page_id: str,
    ref: str,
) -> BrowserActionTarget:
    return BrowserActionTarget(
        session_id=session_id,
        page_id=page_id,
        snapshot_id=snapshot_id,
        ref=ref,
    )


async def _snapshot_with_targets(
    service: BrowserService,
    backend: FakeBrowserBackend,
    targets: tuple[BackendTargetDescriptor, ...],
):
    session = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = f"{_ORIGIN}/form"
    page.title = "Form"
    page.snapshot_text = "\n".join(
        f'- {target.role or "generic"} "{target.name}" [ref={target.ref}]' for target in targets
    )
    page.targets = targets
    snapshot = await service.snapshot(session.session_id, page_id=None)
    return session, page, snapshot


async def _prepare_coordinate_transaction(
    service: BrowserService,
    target: BrowserCoordinateTarget,
    *,
    dialog: BrowserDialogPolicy | None = None,
):
    policy = dialog or BrowserDialogPolicy()
    prepared = await service.prepare_coordinate_commit(target, dialog=policy)
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="browser",
        envelope_sha256="d" * 64,
    )
    return prepared, transaction


async def test_action_context_dispatch_consumes_snapshot_and_returns_fresh_state(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="textbox",
        name="Display name",
        control_kind="text",
        frame_origin=_ORIGIN,
        editable=True,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")

    context = service.action_context(target)
    result = await service.action(
        target,
        BrowserActionRequest(kind="fill", value="ordinary value"),
    )

    assert context.origin == _ORIGIN
    assert context.descriptor.name == "Display name"
    assert result.disposition == "performed"
    assert result.snapshot is not None
    assert result.snapshot.snapshot_id != snapshot.snapshot_id
    assert result.page.latest_action is not None
    assert result.page.latest_action.action_id == result.action_id
    assert len(page.preflights) == 1
    assert len(page.actions) == 1
    assert page.actions[0].action.value == "ordinary value"
    with pytest.raises(BrowserError) as stale:
        service.action_context(target)
    assert stale.value.failure.code == "stale_target"
    await service.aclose()


async def test_local_action_classification_rejects_without_dispatch_and_preserves_target(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptors = (
        BackendTargetDescriptor(
            ref="e1",
            role="button",
            name="Purchase now",
            control_kind="button",
            frame_origin=_ORIGIN,
            consequential=True,
        ),
        BackendTargetDescriptor(
            ref="e2",
            role="textbox",
            name="Password",
            control_kind="text",
            frame_origin=_ORIGIN,
            editable=True,
            protected=True,
        ),
        BackendTargetDescriptor(
            ref="e3",
            role="button",
            name="Upload",
            control_kind="file",
            frame_origin=_ORIGIN,
            file=True,
        ),
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, descriptors)

    click_target = _action_target(
        snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1"
    )
    rejected_click = await service.action(click_target, BrowserActionRequest(kind="click"))
    rejected_fill = await service.action(
        _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e2"),
        BrowserActionRequest(kind="fill", value="must-not-dispatch"),
    )
    rejected_file = await service.action(
        _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e3"),
        BrowserActionRequest(kind="click"),
    )

    assert rejected_click.disposition == "not_performed"
    assert rejected_click.failure is not None
    assert rejected_click.failure.code == "consequential_target"
    assert rejected_fill.failure is not None
    assert rejected_fill.failure.code == "protected_field"
    assert rejected_file.failure is not None
    assert rejected_file.failure.code == "file_control"
    assert page.preflights == []
    assert page.actions == []

    request = BrowserActionRequest(kind="commit", activation="click")
    with pytest.raises(ValueError, match="prepared preflight"):
        await service.action(click_target, request)
    prepared = await service.prepare_commit(
        click_target,
        request,
    )
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="financial",
        envelope_sha256="a" * 64,
    )
    committed = await service.commit_prepared(prepared, transaction)
    assert committed.disposition == "performed"
    assert len(page.actions) == 1
    await service.aclose()


async def test_live_preflight_rejects_changed_target_without_action(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    original = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Continue",
        control_kind="button",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (original,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    page.targets = (
        BackendTargetDescriptor(
            ref="e1",
            role="button",
            name="Delete account",
            control_kind="button",
            frame_origin=_ORIGIN,
            consequential=True,
        ),
    )

    result = await service.action(target, BrowserActionRequest(kind="click"))

    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "incompatible_target"
    assert page.actions == []
    await service.aclose()


async def test_prepared_commit_rejects_changed_destination_and_keeps_transaction_evidence(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Submit application",
        control_kind="button",
        frame_origin=_ORIGIN,
        consequential=True,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    page.effective_destinations = (f"{_ORIGIN}/applications",)
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    prepared = await service.prepare_commit(
        target,
        BrowserActionRequest(kind="commit", activation="click"),
    )
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="browser",
        envelope_sha256="b" * 64,
    )
    page.effective_destinations = (f"{_ORIGIN}/different",)

    result = await service.commit_prepared(prepared, transaction)

    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "stale_target"
    assert result.transaction == transaction
    assert page.actions == []
    current = await service.pages(session.session_id)
    assert current.pages[0].latest_action is not None
    assert current.pages[0].latest_action.transaction == transaction
    await service.aclose()


@pytest.mark.parametrize(
    ("page_url", "frame_origin"),
    [
        ("about:blank", _ORIGIN),
        (f"{_ORIGIN}/form", None),
    ],
)
async def test_prepare_commit_requires_exact_top_and_frame_origins(
    tmp_path: Path,
    page_url: str,
    frame_origin: str | None,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Submit",
        control_kind="button",
        frame_origin=frame_origin,
        consequential=True,
    )
    session = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = page_url
    page.snapshot_text = '- button "Submit" [ref=e1]'
    page.targets = (descriptor,)
    snapshot = await service.snapshot(session.session_id, page_id=None)
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")

    with pytest.raises(BrowserError) as rejected:
        await service.prepare_commit(
            target,
            BrowserActionRequest(kind="commit", activation="click"),
        )

    assert rejected.value.failure.code == "transaction_envelope"
    assert "exact top-level and target-frame origins" in rejected.value.failure.message
    assert page.preflights == []
    assert page.actions == []
    await service.aclose()


async def test_actions_are_fifo_and_second_use_of_consumed_snapshot_is_stale(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Continue",
        control_kind="button",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    page.action_entered = asyncio.Event()
    page.action_release = asyncio.Event()

    first = asyncio.create_task(service.action(target, BrowserActionRequest(kind="click")))
    await asyncio.wait_for(page.action_entered.wait(), timeout=2)
    second = asyncio.create_task(service.action(target, BrowserActionRequest(kind="click")))
    await asyncio.sleep(0)
    assert len(page.actions) == 1

    page.action_release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.disposition == "performed"
    assert second_result.disposition == "not_performed"
    assert second_result.failure is not None
    assert second_result.failure.code == "stale_target"
    assert len(page.actions) == 1
    await service.aclose()


async def test_cancelled_dispatched_action_records_live_in_doubt_evidence(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Continue",
        control_kind="button",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    page.action_entered = asyncio.Event()
    page.action_release = asyncio.Event()
    running = asyncio.create_task(service.action(target, BrowserActionRequest(kind="click")))
    await asyncio.wait_for(page.action_entered.wait(), timeout=2)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    pages = await service.pages(session.session_id)
    assert pages.pages[0].latest_action is not None
    assert pages.pages[0].latest_action.disposition == "in_doubt"
    assert pages.pages[0].latest_action.failure is not None
    assert pages.pages[0].latest_action.failure.code == "action_in_doubt"
    assert len(page.actions) == 1
    with pytest.raises(BrowserError) as stale:
        service.action_context(target)
    assert stale.value.failure.code == "stale_target"
    await service.aclose()


async def test_cancelled_prepared_commit_retains_envelope_evidence(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Submit",
        control_kind="button",
        frame_origin=_ORIGIN,
        consequential=True,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    prepared = await service.prepare_commit(
        target,
        BrowserActionRequest(kind="commit", activation="click"),
    )
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="browser",
        envelope_sha256="d" * 64,
    )
    page.action_entered = asyncio.Event()
    page.action_release = asyncio.Event()
    running = asyncio.create_task(service.commit_prepared(prepared, transaction))
    await asyncio.wait_for(page.action_entered.wait(), timeout=2)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    pages = await service.pages(session.session_id)
    latest = pages.pages[0].latest_action
    assert latest is not None
    assert latest.disposition == "in_doubt"
    assert latest.transaction == transaction
    assert len(page.actions) == 1
    await service.aclose()


async def test_session_close_waits_for_action_and_preserves_performed_evidence(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Continue",
        control_kind="button",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    page.action_entered = asyncio.Event()
    page.action_release = asyncio.Event()
    action = asyncio.create_task(service.action(target, BrowserActionRequest(kind="click")))
    await asyncio.wait_for(page.action_entered.wait(), timeout=2)

    closing = asyncio.create_task(service.close_session(session.session_id))
    await asyncio.sleep(0)
    assert not closing.done()
    page.action_release.set()
    result = await asyncio.wait_for(action, timeout=2)
    closed = await asyncio.wait_for(closing, timeout=2)

    assert result.disposition == "performed"
    assert result.page.latest_action is not None
    assert result.page.latest_action.disposition == "performed"
    assert closed.closed
    assert len(page.actions) == 1
    await service.aclose()


async def test_single_popup_is_selected_and_observed_after_action(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="link",
        name="Open details",
        control_kind="link",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    popup = FakeBrowserPage(
        "popup-1",
        url=f"{_ORIGIN}/details",
        title="Details",
        snapshot='- heading "Popup details"',
    )
    page.action_popups.append(popup)
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")

    result = await service.action(target, BrowserActionRequest(kind="click"))

    assert result.disposition == "performed"
    assert result.postcondition.page_changes.selected_popup_page_id == result.page.page_id
    assert result.postcondition.page_changes.created_page_ids == (result.page.page_id,)
    assert result.page.title == "Details"
    assert result.snapshot is not None
    assert "Popup details" in result.snapshot.content
    assert popup.front_calls == 1
    await service.aclose()


async def test_multiple_popups_are_reported_without_guessing_a_selection(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Open reports",
        control_kind="button",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    page.action_popups.extend(
        [
            FakeBrowserPage("popup-1", url=f"{_ORIGIN}/one", title="One"),
            FakeBrowserPage("popup-2", url=f"{_ORIGIN}/two", title="Two"),
        ]
    )

    result = await service.action(
        _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1"),
        BrowserActionRequest(kind="click"),
    )

    assert result.disposition == "performed"
    assert len(result.postcondition.page_changes.created_page_ids) == 2
    assert result.postcondition.page_changes.selected_popup_page_id is None
    assert result.postcondition.observation_limited
    assert result.postcondition.observation_note is not None
    assert "select one explicitly" in result.postcondition.observation_note
    assert result.snapshot is None
    assert result.page.page_id == snapshot.page.page_id
    await service.aclose()


async def test_popup_discarded_at_page_limit_is_reported_as_created_and_closed(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path, max_pages=1)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="link",
        name="Open details",
        control_kind="link",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    popup = FakeBrowserPage(
        "popup-over-limit",
        url=f"{_ORIGIN}/details",
        title="Details",
    )
    page.action_popups.append(popup)

    result = await service.action(
        _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1"),
        BrowserActionRequest(kind="click"),
    )

    changes = result.postcondition.page_changes
    assert result.disposition == "performed"
    assert popup.closed
    assert len(changes.created_page_ids) == 1
    assert changes.closed_page_ids == changes.created_page_ids
    assert changes.selected_popup_page_id is None
    assert result.postcondition.observation_limited
    assert result.postcondition.observation_note is not None
    assert "page limit" in result.postcondition.observation_note
    assert result.snapshot is None
    assert result.page.page_id == snapshot.page.page_id
    await service.aclose()


async def test_same_url_navigation_advances_generation_once(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Reload",
        control_kind="button",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    before = snapshot.page.navigation_generation
    state = await page.state()
    page.action_outcomes.append(
        BackendActionOutcome(
            disposition="performed",
            dispatch_state="completed",
            state_before=state,
            state_after=state,
            navigation_occurred=True,
        )
    )

    result = await service.action(
        _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1"),
        BrowserActionRequest(kind="click"),
    )

    assert result.page.navigation_generation == before + 1
    assert result.postcondition.navigation_occurred
    await service.aclose()


async def test_handoff_requires_headed_session_and_invalidates_targets(tmp_path: Path) -> None:
    headless_service, headless_backend = _service(tmp_path / "headless")
    descriptor = BackendTargetDescriptor(ref="e1", role="button", name="CAPTCHA")
    session, _page, snapshot = await _snapshot_with_targets(
        headless_service, headless_backend, (descriptor,)
    )
    with pytest.raises(BrowserError) as rejected:
        await headless_service.handoff(
            session.session_id,
            page_id=snapshot.page.page_id,
            reason="captcha",
        )
    assert rejected.value.failure.code == "handoff_required"
    await headless_service.aclose()

    service, backend = _service(tmp_path / "headed", headless=False)
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")

    handoff = await service.handoff(
        session.session_id,
        page_id=snapshot.page.page_id,
        reason="captcha",
    )

    assert handoff.reason == "captcha"
    assert "headed browser" in handoff.prompt
    assert page.front_calls == 1
    with pytest.raises(BrowserError) as stale:
        service.action_context(target)
    assert stale.value.failure.code == "stale_target"
    await service.aclose()


async def test_structured_in_doubt_outcome_is_not_replayed(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Continue",
        control_kind="button",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    state = await page.state()
    failure = BrowserFailure(
        code="action_in_doubt",
        message="browser action timed out after dispatch",
        outcome_uncertain=True,
    )
    page.action_outcomes.append(
        BackendActionOutcome(
            disposition="in_doubt",
            dispatch_state="dispatched",
            state_before=state,
            failure=failure,
        )
    )

    result = await service.action(
        _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1"),
        BrowserActionRequest(kind="click"),
    )

    assert result.disposition == "in_doubt"
    assert result.failure == failure
    assert len(page.actions) == 1
    assert result.postcondition.observation_limited
    await service.aclose()


async def test_prepared_upload_dispatches_exact_bytes_and_consumes_file_target(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Choose reports",
        control_kind="file",
        frame_origin=_ORIGIN,
        file=True,
        multiple=True,
        accept=(".txt", "application/pdf"),
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    prepared = (
        LoadedAttachment(
            filename="report.txt",
            media_type="text/plain",
            content=b"reviewed bytes",
            sha256=hashlib.sha256(b"reviewed bytes").hexdigest(),
            source_path=tmp_path / "source.txt",
        ),
    )

    result = await service.upload(target, prepared)

    assert result.disposition == "performed"
    assert len(page.uploads) == 1
    request, uploaded = page.uploads[0]
    assert request.action.kind == "upload"
    assert uploaded[0].content == b"reviewed bytes"
    assert uploaded[0].sha256 == prepared[0].sha256
    with pytest.raises(BrowserError, match="stale"):
        service.action_context(target)
    await service.aclose()


async def test_prepared_commit_binds_current_execution_uploads(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    file_descriptor = BackendTargetDescriptor(
        ref="e1",
        name="Choose report",
        control_kind="file",
        frame_origin=_ORIGIN,
        file=True,
    )
    commit_descriptor = BackendTargetDescriptor(
        ref="e2",
        role="button",
        name="Submit application",
        control_kind="button",
        frame_origin=_ORIGIN,
        consequential=True,
    )
    session, _page, snapshot = await _snapshot_with_targets(
        service,
        backend,
        (file_descriptor, commit_descriptor),
    )
    content = b"approved application"
    attachment = LoadedAttachment(
        filename="application.txt",
        media_type="text/plain",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        source_path=tmp_path / "application.txt",
    )
    attachment_id = f"task/personal/task_{'1' * 32}/application.txt"

    uploaded = await service.upload(
        _action_target(
            snapshot.snapshot_id,
            session.session_id,
            snapshot.page.page_id,
            "e1",
        ),
        (attachment,),
        attachment_ids=(attachment_id,),
    )
    assert uploaded.disposition == "performed"
    fresh = await service.snapshot(session.session_id, page_id=snapshot.page.page_id)

    prepared = await service.prepare_commit(
        _action_target(
            fresh.snapshot_id,
            session.session_id,
            snapshot.page.page_id,
            "e2",
        ),
        BrowserActionRequest(kind="commit", activation="click"),
    )

    assert tuple(item.id for item in prepared.attachments) == (attachment_id,)
    assert prepared.attachments[0].sha256 == attachment.sha256
    assert prepared.attachments[0].byte_count == len(content)
    await service.aclose()


async def test_upload_rejects_multiple_files_for_single_control_before_dispatch(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        name="Choose one",
        control_kind="file",
        file=True,
        multiple=False,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    files = tuple(
        LoadedAttachment(
            filename=f"{index}.txt",
            media_type="text/plain",
            content=b"x",
            sha256=hashlib.sha256(b"x").hexdigest(),
            source_path=tmp_path / f"{index}.txt",
        )
        for index in range(2)
    )

    result = await service.upload(target, files)

    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "incompatible_target"
    assert page.uploads == []
    assert service.action_context(target).descriptor.file
    await service.aclose()


async def test_upload_rejects_snapshot_after_live_page_navigation(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        name="Choose one",
        control_kind="file",
        file=True,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    page.url = f"{_ORIGIN}/different-page"
    body = b"reviewed"

    result = await service.upload(
        target,
        (
            LoadedAttachment(
                filename="reviewed.txt",
                media_type="text/plain",
                content=body,
                sha256=hashlib.sha256(body).hexdigest(),
                source_path=tmp_path / "reviewed.txt",
            ),
        ),
    )

    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "stale_target"
    assert page.uploads == []
    assert result.page.navigation_generation == snapshot.page.navigation_generation + 1
    await service.aclose()


async def test_download_rejects_snapshot_after_live_page_navigation(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(ref="e1", role="link", name="Download")
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    page.url = f"{_ORIGIN}/different-page"

    result = await service.download(target)

    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "stale_target"
    assert page.downloads == []
    assert result.page.navigation_generation == snapshot.page.navigation_generation + 1
    await service.aclose()


async def test_coordinate_commit_rejects_visual_after_live_page_navigation(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="d1",
        role="canvas",
        name="Visual",
        frame_origin=_ORIGIN,
    )
    session = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = f"{_ORIGIN}/canvas"
    page.visual_capture = _visual_capture(descriptor)
    page.coordinate_target = descriptor
    visual = await service.visual_snapshot(session.session_id, page_id=None)
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=visual.page.page_id,
        screenshot_id=visual.snapshot_id,
        x=10,
        y=10,
    )
    prepared, transaction = await _prepare_coordinate_transaction(service, target)
    page.url = f"{_ORIGIN}/different-page"

    result = await service.coordinate_commit_prepared(prepared, transaction)

    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "stale_target"
    assert page.coordinates == []
    assert result.page.navigation_generation == visual.page.navigation_generation + 1
    await service.aclose()


async def test_owned_download_publishes_digest_bound_logical_reference(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="link",
        name="Download report",
        control_kind="link",
        frame_origin=_ORIGIN,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    options = backend.options[0]
    assert hasattr(options, "download_temp_dir")
    temporary = options.download_temp_dir / "attempt.download"  # type: ignore[union-attr]
    temporary.write_bytes(b"download body")
    state = await page.state()
    page.download_outcomes.append(
        BackendDownloadOutcome(
            action=BackendActionOutcome(
                disposition="performed",
                dispatch_state="completed",
                state_before=state,
                state_after=state,
            ),
            download=BackendDownload(
                temporary_path=temporary,
                suggested_filename="../../unsafe report.txt",
                media_type="text/plain",
            ),
        )
    )

    result = await service.download(
        _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    )

    assert result.disposition == "performed"
    assert result.download is not None
    assert result.download.profile == "personal"
    assert result.download.filename == "unsafe report.txt"
    assert result.download.sha256 == hashlib.sha256(b"download body").hexdigest()
    final = browser_download_path(service._settings, result.download)
    assert final.read_bytes() == b"download body"
    assert not temporary.exists()
    assert not Path(service._settings.project_data_dir).exists()
    await service.aclose()
    assert final.read_bytes() == b"download body"


async def test_download_overflow_is_in_doubt_and_never_published(tmp_path: Path) -> None:
    service, backend = _service(tmp_path, download_file_byte_limit=4)
    descriptor = BackendTargetDescriptor(ref="e1", role="link", name="Download")
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    options = backend.options[0]
    temporary = options.download_temp_dir / "large.download"  # type: ignore[union-attr]
    temporary.write_bytes(b"too large")
    state = await page.state()
    page.download_outcomes.append(
        BackendDownloadOutcome(
            action=BackendActionOutcome(
                disposition="performed",
                dispatch_state="completed",
                state_before=state,
                state_after=state,
            ),
            download=BackendDownload(temporary, "large.bin"),
        )
    )

    result = await service.download(
        _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    )

    assert result.disposition == "in_doubt"
    assert result.download is None
    assert result.failure is not None
    assert result.failure.code == "download_too_large"
    assert not temporary.exists()
    await service.aclose()


async def test_visual_generation_maps_coordinates_and_is_consumed_once(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    semantic = BackendTargetDescriptor(ref="e1", role="button", name="Semantic")
    session, page, old_snapshot = await _snapshot_with_targets(service, backend, (semantic,))
    visual_descriptor = BackendTargetDescriptor(
        ref="d1",
        role="canvas",
        name="Visual control",
        control_kind="other",
        frame_origin=_ORIGIN,
    )
    page.visual_capture = _visual_capture(visual_descriptor)
    page.coordinate_target = visual_descriptor

    capture = await service.visual_snapshot(session.session_id, page_id=old_snapshot.page.page_id)
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=99,
        y=49,
    )
    context = service.coordinate_context(target)

    assert capture.candidates[0].target.ref == "d1"
    assert context.css_x == 99
    assert context.css_y == 49
    with pytest.raises(BrowserError, match="stale"):
        service.action_context(
            _action_target(
                old_snapshot.snapshot_id,
                session.session_id,
                old_snapshot.page.page_id,
                "e1",
            )
        )

    prepared, transaction = await _prepare_coordinate_transaction(service, target)
    result = await service.coordinate_commit_prepared(prepared, transaction)

    assert result.disposition == "performed"
    assert len(page.coordinates) == 1
    assert page.coordinates[0].x == 99
    assert page.coordinates[0].y == 49
    assert page.coordinates[0].masked_base_sha256 == context.masked_base_sha256
    with pytest.raises(BrowserError, match="stale"):
        service.coordinate_context(target)
    await service.aclose()


async def test_prepared_coordinate_commit_rejects_changed_destination(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    session = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = f"{_ORIGIN}/canvas"
    page.visual_capture = _visual_capture(
        BackendTargetDescriptor(ref="d1", role="button", name="Submit")
    )
    page.coordinate_target = BackendTargetDescriptor(
        ref="d1",
        role="button",
        name="Submit",
        control_kind="button",
        frame_origin=_ORIGIN,
        consequential=True,
    )
    page.coordinate_destinations = (f"{_ORIGIN}/forms",)
    capture = await service.visual_snapshot(session.session_id, page_id=None)
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=20,
        y=10,
    )
    prepared = await service.prepare_coordinate_commit(
        target,
        dialog=BrowserDialogPolicy(response="accept"),
    )
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="browser",
        envelope_sha256="c" * 64,
    )
    page.coordinate_destinations = (f"{_ORIGIN}/changed",)

    result = await service.coordinate_commit_prepared(prepared, transaction)

    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "stale_target"
    assert result.transaction == transaction
    assert page.coordinates == []
    await service.aclose()


@pytest.mark.parametrize(
    ("page_url", "frame_origin"),
    [
        ("about:blank", _ORIGIN),
        (f"{_ORIGIN}/canvas", None),
    ],
)
async def test_prepare_coordinate_commit_requires_exact_top_and_frame_origins(
    tmp_path: Path,
    page_url: str,
    frame_origin: str | None,
) -> None:
    service, backend = _service(tmp_path)
    session = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = page_url
    page.visual_capture = _visual_capture(
        BackendTargetDescriptor(ref="d1", role="button", name="Submit")
    )
    page.coordinate_target = BackendTargetDescriptor(
        ref="d1",
        role="button",
        name="Submit",
        control_kind="button",
        frame_origin=frame_origin,
        consequential=True,
    )
    capture = await service.visual_snapshot(session.session_id, page_id=None)
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=20,
        y=10,
    )

    with pytest.raises(BrowserError) as rejected:
        await service.prepare_coordinate_commit(target, dialog=BrowserDialogPolicy())

    assert rejected.value.failure.code == "transaction_envelope"
    assert "exact top-level and target-frame origins" in rejected.value.failure.message
    assert page.coordinates == []
    await service.aclose()


async def test_coordinate_backend_stale_pixels_fail_without_dispatch_evidence(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="d1",
        role="canvas",
        name="Visual",
        frame_origin=_ORIGIN,
    )
    session = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = f"{_ORIGIN}/canvas"
    page.visual_capture = _visual_capture(descriptor)
    page.coordinate_target = descriptor
    capture = await service.visual_snapshot(session.session_id, page_id=None)
    state = await page.state()
    page.coordinate_outcomes.append(
        BackendActionOutcome(
            disposition="not_performed",
            dispatch_state="not_dispatched",
            state_before=state,
            state_after=state,
            failure=BrowserFailure(
                code="stale_target",
                message="browser viewport pixels changed",
            ),
        )
    )
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=10,
        y=10,
    )

    prepared, transaction = await _prepare_coordinate_transaction(service, target)
    result = await service.coordinate_commit_prepared(prepared, transaction)

    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "stale_target"
    with pytest.raises(BrowserError, match="stale"):
        service.coordinate_context(target)
    await service.aclose()


async def test_unexpected_coordinate_backend_failure_records_live_in_doubt_evidence(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="d1",
        role="button",
        name="Submit",
        control_kind="button",
        frame_origin=_ORIGIN,
        consequential=True,
    )
    session = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = f"{_ORIGIN}/form"
    page.visual_capture = _visual_capture(descriptor)
    page.coordinate_target = descriptor
    capture = await service.visual_snapshot(session.session_id, page_id=None)
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=10,
        y=10,
    )
    prepared, transaction = await _prepare_coordinate_transaction(service, target)

    async def fail_backend(*_args: object, **_kwargs: object) -> BackendActionOutcome:
        raise RuntimeError("synthetic backend failure")

    page.perform_coordinate_commit = fail_backend  # type: ignore[method-assign]
    result = await service.coordinate_commit_prepared(prepared, transaction)

    assert result.disposition == "in_doubt"
    assert result.failure is not None
    assert result.failure.code == "action_in_doubt"
    assert result.transaction == transaction
    latest = (await service.pages(session.session_id)).pages[0].latest_action
    assert latest is not None
    assert latest.disposition == "in_doubt"
    assert latest.transaction == transaction
    with pytest.raises(BrowserError, match="stale"):
        service.coordinate_context(target)
    await service.aclose()


async def test_special_effect_post_observation_failure_is_never_reported_performed(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="d1",
        role="canvas",
        name="Visual",
        frame_origin=_ORIGIN,
    )
    session = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = f"{_ORIGIN}/canvas"
    page.visual_capture = _visual_capture(descriptor)
    page.coordinate_target = descriptor
    capture = await service.visual_snapshot(session.session_id, page_id=None)
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=10,
        y=10,
    )
    prepared, transaction = await _prepare_coordinate_transaction(service, target)

    original_sync = service._sync_pages

    async def fail_sync(
        entry: Any,
        *,
        discard_blocked_owned: bool = False,
    ) -> Any:
        if discard_blocked_owned:
            raise RuntimeError("observation failed")
        return await original_sync(entry)

    service._sync_pages = fail_sync  # type: ignore[method-assign]
    result = await service.coordinate_commit_prepared(prepared, transaction)

    assert result.disposition == "in_doubt"
    assert result.failure is not None
    assert result.failure.code == "action_in_doubt"
    assert result.failure.outcome_uncertain
    assert len(page.coordinates) == 1
    await service.aclose()


async def test_cancelled_upload_records_in_doubt_and_consumes_snapshot(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Upload",
        control_kind="file",
        file=True,
    )
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    page.upload_entered = asyncio.Event()
    page.upload_release = asyncio.Event()
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    body = b"reviewed"
    task = asyncio.create_task(
        service.upload(
            target,
            (
                LoadedAttachment(
                    filename="reviewed.txt",
                    media_type="text/plain",
                    content=body,
                    sha256=hashlib.sha256(body).hexdigest(),
                    source_path=tmp_path / "reviewed.txt",
                ),
            ),
        )
    )
    await page.upload_entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    latest = (await service.pages(session.session_id)).pages[0].latest_action
    assert latest is not None
    assert latest.kind == "upload"
    assert latest.disposition == "in_doubt"
    with pytest.raises(BrowserError, match="stale"):
        service.action_context(target)
    await service.aclose()


async def test_cancelled_download_records_in_doubt_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(ref="e1", role="link", name="Download")
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    page.download_entered = asyncio.Event()
    page.download_release = asyncio.Event()
    target = _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
    task = asyncio.create_task(service.download(target))
    await page.download_entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    latest = (await service.pages(session.session_id)).pages[0].latest_action
    assert latest is not None
    assert latest.kind == "download"
    assert latest.disposition == "in_doubt"
    durable = (
        Path(service._settings.user_data_dir)
        / "profiles"
        / "personal"
        / service._settings.browser.download_dir
    )
    assert not durable.exists()
    await service.aclose()


async def test_cancellation_joins_download_publication_before_cleanup_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(ref="e1", role="link", name="Download")
    session, page, snapshot = await _snapshot_with_targets(service, backend, (descriptor,))
    options = backend.options[0]
    assert hasattr(options, "download_temp_dir")
    temporary = options.download_temp_dir / "publishing.download"  # type: ignore[union-attr]
    temporary.write_bytes(b"publication body")
    state = await page.state()
    page.download_outcomes.append(
        BackendDownloadOutcome(
            action=BackendActionOutcome(
                disposition="performed",
                dispatch_state="completed",
                state_before=state,
                state_after=state,
            ),
            download=BackendDownload(temporary, "publication.txt", "text/plain"),
        )
    )
    started = asyncio.Event()
    release = asyncio.Event()
    published = []
    loop = asyncio.get_running_loop()
    original = service._publish_download

    def delayed_publication(*args: Any):
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
        reference = original(*args)
        published.append(reference)
        return reference

    monkeypatch.setattr(service, "_publish_download", delayed_publication)
    task = asyncio.create_task(
        service.download(
            _action_target(snapshot.snapshot_id, session.session_id, snapshot.page.page_id, "e1")
        )
    )
    await started.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert temporary.exists()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(published) == 1
    final = browser_download_path(service._settings, published[0])
    assert final.read_bytes() == b"publication body"
    assert not temporary.exists()
    latest = (await service.pages(session.session_id)).pages[0].latest_action
    assert latest is not None
    assert latest.kind == "download"
    assert latest.disposition == "in_doubt"
    assert latest.failure is not None
    assert latest.failure.outcome_uncertain
    await service.aclose()


async def test_cancelled_coordinate_commit_records_in_doubt(tmp_path: Path) -> None:
    service, backend = _service(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="d1",
        role="canvas",
        name="Visual",
        frame_origin=_ORIGIN,
    )
    session = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = f"{_ORIGIN}/canvas"
    page.visual_capture = _visual_capture(descriptor)
    page.coordinate_target = descriptor
    page.coordinate_entered = asyncio.Event()
    page.coordinate_release = asyncio.Event()
    capture = await service.visual_snapshot(session.session_id, page_id=None)
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=10,
        y=10,
    )
    prepared, transaction = await _prepare_coordinate_transaction(service, target)
    task = asyncio.create_task(service.coordinate_commit_prepared(prepared, transaction))
    await page.coordinate_entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    latest = (await service.pages(session.session_id)).pages[0].latest_action
    assert latest is not None
    assert latest.kind == "coordinate_commit"
    assert latest.disposition == "in_doubt"
    with pytest.raises(BrowserError, match="stale"):
        service.coordinate_context(target)
    await service.aclose()
