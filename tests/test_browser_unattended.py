"""Background browser guard, inventory, and semantic-first fallback contracts."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image
from pydantic import TypeAdapter, ValidationError

from browser_support import FakeBrowserBackend, FakeBrowserPage, FakeBrowserSession, fake_executable
from ricky.browser import background_browser_tools, browser_tool_descriptors
from ricky.browser.backend import (
    BackendBoundingBox,
    BackendTargetDescriptor,
    BackendViewport,
    BackendVisualCandidate,
    BackendVisualSnapshot,
)
from ricky.browser.runtime_guard import (
    BrowserBudgetKind,
    BrowserExecutionGuard,
    BrowserGuardFacts,
    BrowserRuntimeEvidence,
)
from ricky.browser.service import BrowserService
from ricky.browser.types import (
    BrowserCoordinateTarget,
    BrowserDialogPolicy,
    BrowserError,
    CoordinateFallbackEvidence,
)
from ricky.config import RickySettings

_ORIGIN = "https://127.0.0.1:9443"


class RecordingBrowserGuard:
    execution_id = "execution_" + "e" * 32

    def __init__(self, *, private_origins: tuple[str, ...] = (_ORIGIN,)) -> None:
        self.private_origin_ceiling = private_origins
        self.controlled_page_ceiling = 8
        self.checks: list[BrowserGuardFacts] = []
        self.reservations: list[tuple[BrowserBudgetKind, int, BrowserGuardFacts]] = []
        self.evidence: list[BrowserRuntimeEvidence] = []
        self.page_capacity: list[tuple[int, BrowserGuardFacts]] = []
        self.page_releases: list[tuple[int, BrowserGuardFacts]] = []

    def bind_effect_action(self, action_id: str, action_key: str) -> None:
        del action_id, action_key

    async def settle_effect_action(self, action_id: str) -> None:
        del action_id

    async def check(self, facts: BrowserGuardFacts) -> None:
        self.checks.append(facts)

    async def reserve(
        self,
        kind: BrowserBudgetKind,
        amount: int,
        facts: BrowserGuardFacts,
    ) -> None:
        self.reservations.append((kind, amount, facts))

    async def record(self, evidence: BrowserRuntimeEvidence) -> None:
        self.evidence.append(evidence)

    async def reserve_possible_pages(
        self,
        maximum_creation_count: int,
        facts: BrowserGuardFacts,
    ) -> None:
        self.page_capacity.append((maximum_creation_count, facts))

    async def release_controlled_pages(
        self,
        amount: int,
        facts: BrowserGuardFacts,
    ) -> None:
        self.page_releases.append((amount, facts))


def _service(
    tmp_path: Path,
    page: FakeBrowserPage | None = None,
) -> tuple[BrowserService, RecordingBrowserGuard, FakeBrowserPage]:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "browser": {
                "enabled": True,
                "headless": True,
                "allowed_private_origins": [_ORIGIN],
            },
            "profile_configs": {
                "personal": {"browser": {"screenshot_allowed_providers": ["openrouter"]}}
            },
        }
    )
    selected_page = page or FakeBrowserPage(url=f"{_ORIGIN}/fixture")
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([selected_page]))
    guard = RecordingBrowserGuard()
    service = BrowserService(
        settings,
        scope=settings.resolve_profile_scope(),
        backend=backend,
        executable_path=fake_executable(tmp_path),
        runtime_guard=guard,
    )
    return service, guard, selected_page


def _visual(descriptor: BackendTargetDescriptor) -> BackendVisualSnapshot:
    output = BytesIO()
    Image.new("RGB", (100, 50), (230, 230, 230)).save(output, format="PNG")
    png = output.getvalue()
    return BackendVisualSnapshot(
        png=png,
        masked_base_sha256=hashlib.sha256(png).hexdigest(),
        viewport=BackendViewport(
            width=100,
            height=50,
            scroll_x=0,
            scroll_y=0,
            device_scale_factor=1,
        ),
        candidates=(
            BackendVisualCandidate(
                descriptor=descriptor,
                bounding_box=BackendBoundingBox(x=10, y=5, width=20, height=10),
            ),
        ),
    )


def test_guard_models_are_strict_json_round_trip_boundaries() -> None:
    fallback = CoordinateFallbackEvidence(
        semantic_snapshot_id="browser_snapshot_" + "c" * 32,
        reason="no_supported_semantic_target",
    )
    facts = BrowserGuardFacts(
        tool_name="browser_coordinate_commit",
        session_id="browser_session_" + "a" * 32,
        page_id="browser_page_" + "b" * 32,
        navigation_generation=3,
        controlled_page_count=1,
        provider="openrouter",
        snapshot_id="browser_snapshot_" + "d" * 32,
        target_ref="d1",
        action_kind="coordinate_commit",
        coordinate_fallback=fallback,
    )
    evidence = BrowserRuntimeEvidence(facts=facts, disposition="not_performed")

    assert BrowserRuntimeEvidence.model_validate_json(evidence.model_dump_json()) == evidence
    assert json.loads(evidence.model_dump_json())["facts"]["provider"] == "openrouter"
    with pytest.raises(ValidationError):
        BrowserGuardFacts.model_validate({**facts.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        BrowserGuardFacts(
            tool_name="browser_click",
            coordinate_fallback=fallback,
        )


def test_background_inventory_matches_real_tools_and_excludes_handoff(tmp_path: Path) -> None:
    service, _guard, _page = _service(tmp_path)
    descriptors = browser_tool_descriptors()
    descriptor_names = tuple(tool.name for tool in descriptors)

    assert "browser_handoff" not in descriptor_names
    assert all(cast(Any, tool).unattended == "allowed" for tool in descriptors)
    selected = frozenset({"browser_resources", "browser_session_open", "browser_snapshot"})
    actual = background_browser_tools(service, mode="read_only", allowed_tools=selected)
    assert tuple(tool.name for tool in actual) == tuple(
        name for name in descriptor_names if name in selected
    )
    assert tuple(type(tool) for tool in actual) == tuple(
        type(tool) for tool in descriptors if tool.name in selected
    )
    assert all(cast(Any, tool).review_mode == "policy" for tool in actual)

    async def resolve_attachments(_ids: tuple[str, ...]):
        return ()

    formerly_fresh = background_browser_tools(
        service,
        mode="transaction",
        allowed_tools=frozenset(
            {
                "browser_session_open_resource",
                "browser_upload",
                "browser_download",
                "browser_coordinate_click",
            }
        ),
        attachment_resolver=resolve_attachments,
    )
    assert all(cast(Any, tool).review_mode == "policy" for tool in formerly_fresh)
    with pytest.raises(ValueError, match="read_only browser execution"):
        background_browser_tools(
            service,
            mode="read_only",
            allowed_tools=frozenset({"browser_click"}),
        )
    with pytest.raises(ValueError, match="dependencies are unavailable"):
        background_browser_tools(
            service,
            mode="transaction",
            allowed_tools=frozenset({"browser_fill_protected"}),
        )
    assert isinstance(service._runtime_guard, BrowserExecutionGuard)  # type: ignore[attr-defined]


async def test_guard_observes_and_reserves_background_read_operations(tmp_path: Path) -> None:
    service, guard, page = _service(tmp_path)
    page.snapshot_text = '- button "Continue" [ref=e1]'
    page.targets = (
        BackendTargetDescriptor(
            ref="e1",
            role="button",
            name="Continue",
            control_kind="button",
            frame_origin=_ORIGIN,
        ),
    )

    session = await service.open_session()
    await service.navigate(session.session_id, page_id=None, url=f"{_ORIGIN}/next")
    snapshot = await service.snapshot(session.session_id, page_id=None)

    assert snapshot.targets[0].ref == "e1"
    assert [kind for kind, _amount, _facts in guard.reservations] == [
        "session_starts",
        "controlled_pages",
        "navigations",
        "semantic_observations",
    ]
    assert guard.reservations[2][2].effective_destination_origins == (_ORIGIN,)
    assert guard.reservations[3][2].controlled_page_count == 1
    assert [item.facts.tool_name for item in guard.evidence] == [
        "browser_pages",
        "browser_session_open",
        "browser_navigate",
        "browser_snapshot",
    ]
    assert all(item.disposition == "completed" for item in guard.evidence)
    await service.aclose()


async def test_background_policy_intersects_installation_and_execution_private_origins(
    tmp_path: Path,
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "browser": {
                "enabled": True,
                "headless": True,
                "allowed_private_origins": [_ORIGIN],
            },
        }
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([FakeBrowserPage()]))
    service = BrowserService(
        settings,
        scope=settings.resolve_profile_scope(),
        backend=backend,
        executable_path=fake_executable(tmp_path),
        runtime_guard=RecordingBrowserGuard(private_origins=()),
    )
    session = await service.open_session()

    with pytest.raises(BrowserError) as blocked:
        await service.navigate(session.session_id, page_id=None, url=f"{_ORIGIN}/private")

    assert blocked.value.failure.code == "destination_blocked"
    await service.aclose()


async def test_visual_disclosure_is_bound_to_source_profile_and_provider(tmp_path: Path) -> None:
    service, guard, page = _service(tmp_path)
    descriptor = BackendTargetDescriptor(ref="d1", role="canvas", name="Canvas")
    page.visual_capture = _visual(descriptor)
    session = await service.open_session()

    with pytest.raises(BrowserError) as missing:
        await service.visual_snapshot(session.session_id, page_id=None)
    assert missing.value.failure.code == "screenshot_denied"
    capture = await service.visual_snapshot(
        session.session_id,
        page_id=None,
        provider="openrouter",
    )
    await service.revalidate_visual_disclosure(
        session.session_id,
        capture.page.page_id,
        capture.snapshot_id,
        provider="openrouter",
    )

    assert guard.reservations[-1][0] == "visual_observations"
    assert guard.reservations[-1][2].provider == "openrouter"
    profile_browser = service._settings.profile_configs[  # type: ignore[attr-defined]
        "personal"
    ].browser
    assert profile_browser is not None
    profile_browser.screenshot_allowed_providers = []
    with pytest.raises(BrowserError) as revoked:
        await service.revalidate_visual_disclosure(
            session.session_id,
            capture.page.page_id,
            capture.snapshot_id,
            provider="openrouter",
        )
    assert revoked.value.failure.code == "screenshot_denied"
    await service.aclose()


async def test_coordinate_commit_redirects_to_current_semantic_target(tmp_path: Path) -> None:
    semantic_target = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Continue",
        control_kind="button",
        frame_origin=_ORIGIN,
        consequential=True,
    )
    coordinate_target = BackendTargetDescriptor(
        ref="d1",
        role="button",
        name="Continue",
        control_kind="button",
        frame_origin=_ORIGIN,
        consequential=True,
    )
    page = FakeBrowserPage(
        url=f"{_ORIGIN}/checkout",
        snapshot='- button "Continue" [ref=e1]',
        targets=(semantic_target,),
    )
    page.visual_capture = _visual(coordinate_target)
    page.coordinate_target = coordinate_target
    page.coordinate_equivalent_semantic_ref = "e1"
    service, _guard, _page = _service(tmp_path, page)
    session = await service.open_session()
    await service.snapshot(session.session_id, page_id=None)
    capture = await service.visual_snapshot(
        session.session_id,
        page_id=None,
        provider="openrouter",
    )
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=15.25,
        y=10.75,
    )

    with pytest.raises(BrowserError) as redirect:
        await service.prepare_coordinate_commit(target, dialog=BrowserDialogPolicy())

    assert redirect.value.failure.code == "semantic_target_available"
    assert redirect.value.failure.replacement_target is not None
    assert redirect.value.failure.replacement_target.ref == "e1"
    assert page.coordinates == []
    await service.aclose()


async def test_coordinate_commit_uses_harness_fallback_and_exact_binding(tmp_path: Path) -> None:
    canvas = BackendTargetDescriptor(
        ref="d1",
        role="canvas",
        name="Position-sensitive checkout",
        frame_origin=_ORIGIN,
        consequential=True,
    )
    page = FakeBrowserPage(url=f"{_ORIGIN}/checkout", snapshot="- document")
    page.visual_capture = _visual(canvas)
    page.coordinate_target = canvas
    service, guard, _page = _service(tmp_path, page)
    session = await service.open_session()
    await service.snapshot(session.session_id, page_id=None)
    capture = await service.visual_snapshot(
        session.session_id,
        page_id=None,
        provider="openrouter",
    )
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=15.25,
        y=10.75,
    )
    prepared = await service.prepare_coordinate_commit(target, dialog=BrowserDialogPolicy())
    assert prepared.fallback is not None
    assert prepared.fallback.reason == "custom_rendered_target"
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="browser",
        envelope_sha256="f" * 64,
    )

    result = await service.coordinate_commit_prepared(prepared, transaction)

    assert result.disposition == "performed"
    assert len(page.coordinates) == 1
    assert page.coordinate_preflights[0].x == 15.25
    assert page.coordinate_preflights[0].y == 10.75
    assert page.coordinates[0].x == 15.25
    assert page.coordinates[0].y == 10.75
    commit_reservation = next(
        facts for kind, _amount, facts in guard.reservations if kind == "transaction_commits"
    )
    assert commit_reservation.provider == "openrouter"
    assert commit_reservation.coordinate_fallback == prepared.fallback
    assert commit_reservation.transaction == transaction
    assert any(kind == "navigations" for kind, _amount, _facts in guard.reservations)
    assert guard.page_releases[-1][0] == 7
    assert guard.evidence[-1].disposition == "performed"
    await service.aclose()
    assert guard.page_releases[-1][0] == 1


async def test_ordinary_coordinate_click_is_semantic_first_and_nonconsequential(
    tmp_path: Path,
) -> None:
    canvas = BackendTargetDescriptor(
        ref="d1",
        role="canvas",
        name="Position-sensitive menu",
        frame_origin=_ORIGIN,
    )
    page = FakeBrowserPage(url=f"{_ORIGIN}/menu", snapshot="- document")
    page.visual_capture = _visual(canvas)
    page.coordinate_target = canvas
    service, guard, _page = _service(tmp_path, page)
    session = await service.open_session()
    await service.snapshot(session.session_id, page_id=None)
    capture = await service.visual_snapshot(
        session.session_id,
        page_id=None,
        provider="openrouter",
    )
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=15,
        y=10,
    )

    prepared = await service.prepare_coordinate_click(target)
    result = await service.coordinate_click_prepared(prepared)

    assert prepared.fallback.reason == "custom_rendered_target"
    assert result.kind == "coordinate_click"
    assert result.transaction is None
    interaction = next(
        facts for kind, _amount, facts in guard.reservations if kind == "interactions"
    )
    assert interaction.tool_name == "browser_coordinate_click"
    assert interaction.coordinate_fallback == prepared.fallback
    assert len(page.coordinates) == 1
    await service.aclose()


async def test_ordinary_coordinate_click_cannot_bypass_transaction_commit(
    tmp_path: Path,
) -> None:
    consequential = BackendTargetDescriptor(
        ref="d1",
        role="canvas",
        name="Pay now",
        frame_origin=_ORIGIN,
        consequential=True,
    )
    page = FakeBrowserPage(url=f"{_ORIGIN}/checkout", snapshot="- document")
    page.visual_capture = _visual(consequential)
    page.coordinate_target = consequential
    service, _guard, _page = _service(tmp_path, page)
    session = await service.open_session()
    await service.snapshot(session.session_id, page_id=None)
    capture = await service.visual_snapshot(
        session.session_id,
        page_id=None,
        provider="openrouter",
    )
    target = BrowserCoordinateTarget(
        session_id=session.session_id,
        page_id=capture.page.page_id,
        screenshot_id=capture.snapshot_id,
        x=15,
        y=10,
    )

    with pytest.raises(BrowserError) as rejected:
        await service.prepare_coordinate_click(target)

    assert rejected.value.failure.code == "consequential_target"
    assert page.coordinates == []
    await service.aclose()


async def test_background_persistent_open_enforces_restored_page_ceiling(
    tmp_path: Path,
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "browser": {
                "enabled": True,
                "headless": True,
                "allowed_private_origins": [_ORIGIN],
            },
            "profile_configs": {
                "personal": {
                    "browser": {
                        "resources": {
                            "research": {
                                "kind": "persistent",
                                "description": "Dedicated research profile.",
                                "headless": True,
                            }
                        }
                    }
                }
            },
        }
    )
    backend = FakeBrowserBackend()
    first = FakeBrowserPage(url=f"{_ORIGIN}/one")
    second = FakeBrowserPage("page-2", url=f"{_ORIGIN}/two")
    backend.pending_sessions.append(FakeBrowserSession([first, second]))
    guard = RecordingBrowserGuard()
    guard.controlled_page_ceiling = 1
    service = BrowserService(
        settings,
        scope=settings.resolve_profile_scope(),
        backend=backend,
        executable_path=fake_executable(tmp_path),
        runtime_guard=guard,
    )

    with pytest.raises(BrowserError) as exceeded:
        await service.open_resource("personal/research")

    assert exceeded.value.failure.code == "page_limit"
    assert second.closed is True
    assert sum(amount for amount, _facts in guard.page_releases) == 1
    await service.aclose()


def test_budget_kind_contract_matches_durable_execution_operations() -> None:
    from ricky.executions.browser import BrowserBudgetOperation

    guard_kinds = set(TypeAdapter(BrowserBudgetKind).json_schema()["enum"])
    durable_kinds = set(TypeAdapter(BrowserBudgetOperation).json_schema()["enum"])
    assert guard_kinds == durable_kinds - {"parked_browsers"}
