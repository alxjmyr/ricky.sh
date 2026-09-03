"""Browser service ownership, identity, bounds, and coordination tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr

from browser_support import (
    FakeBrowserBackend,
    FakeBrowserPage,
    FakeBrowserSession,
    fake_executable,
)
from ricky.browser.backend import BackendTargetDescriptor, BrowserLaunchOptions
from ricky.browser.service import BrowserService
from ricky.browser.types import BrowserActionRequest, BrowserActionTarget, BrowserError
from ricky.config import RickySettings
from ricky.profiles import ProfileResourceRef
from ricky.protected_values import (
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedUseRequest,
    ProtectedValueBroker,
)

_LOCAL_ORIGIN = "http://127.0.0.1:8765"
_SECURE_LOCAL_ORIGIN = "https://127.0.0.1:9443"
_PROTECTED_SENTINEL = "browser-protected-sentinel-5927"


def _settings(
    tmp_path: Path,
    *,
    max_sessions: int = 1,
    max_pages: int = 8,
    snapshot_char_limit: int = 1_000,
) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "browser": {
                "enabled": True,
                "headless": True,
                "max_sessions": max_sessions,
                "max_pages": max_pages,
                "snapshot_depth": 7,
                "snapshot_char_limit": snapshot_char_limit,
                "allowed_private_origins": [_LOCAL_ORIGIN, _SECURE_LOCAL_ORIGIN],
            },
            "protected_values": {
                "enabled": True,
                "argon2_iterations": 1,
                "argon2_lanes": 1,
                "argon2_memory_kib": 8192,
            },
        }
    )


async def _protected_broker(tmp_path: Path, settings: RickySettings) -> ProtectedValueBroker:
    broker = ProtectedValueBroker(
        settings,
        scope=settings.resolve_profile_scope(),
        consumer_ids=frozenset({"browser.fill"}),
    )
    await broker.initialize("personal", SecretStr("test-passphrase"))
    await broker.unlock("personal", SecretStr("test-passphrase"))
    await broker.create(
        profile="personal",
        name="fixture-login",
        kind="credential",
        label="Fixture login",
        description="Synthetic browser acceptance fixture.",
        fields=(
            ProtectedFieldDescriptor(
                name="password",
                label="Fixture password",
                mode="stored",
                compatible_controls=("password",),
            ),
        ),
        policy=ProtectedDestinationPolicy(
            mode="strict",
            authored_origins=(_SECURE_LOCAL_ORIGIN,),
        ),
        values={"password": SecretStr(_PROTECTED_SENTINEL)},
    )
    return broker


async def test_protected_fill_uses_dedicated_request_and_safe_evidence(
    tmp_path: Path,
) -> None:
    target_descriptor = BackendTargetDescriptor(
        ref="e1",
        role="textbox",
        name="Password",
        control_kind="text",
        frame_origin=_SECURE_LOCAL_ORIGIN,
        editable=True,
        protected=True,
        protected_kind="password",
    )
    page = FakeBrowserPage(
        url=_SECURE_LOCAL_ORIGIN + "/login",
        snapshot='- textbox "Password" [ref=e1]',
        targets=(target_descriptor,),
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))
    service, _backend, settings = _service(tmp_path, backend)
    broker = await _protected_broker(tmp_path, settings)
    opened = await service.open_session()
    snapshot = await service.snapshot(opened.session_id, page_id=None)
    target = BrowserActionTarget.model_validate(
        snapshot.targets[0].model_dump(exclude={"navigation_generation"}), strict=True
    )
    context = await service.protected_action_context(target)
    assert context.descriptor.protected_kind == "password"
    material = await broker.prepare(
        ProtectedUseRequest(
            ref=ProfileResourceRef(profile="personal", name="fixture-login"),
            field="password",
            consumer_id="browser.fill",
            control_kind="password",
            top_level_origin=_SECURE_LOCAL_ORIGIN,
            frame_origin=_SECURE_LOCAL_ORIGIN,
            occurrence=service.protected_occurrence(target),
        )
    )

    result = await service.protected_fill(target, material, broker)

    assert result.disposition == "performed"
    assert result.kind == "protected_fill"
    assert len(page.protected_fills) == 1
    assert page.protected_fills[0].value.get_secret_value() == _PROTECTED_SENTINEL
    assert _PROTECTED_SENTINEL not in result.model_dump_json()
    assert result.page.latest_action is not None
    assert result.page.latest_action.protected_ref == material.descriptor.ref
    assert result.page.latest_action.protected_field == "password"
    await broker.aclose()
    await service.aclose()


async def test_payment_card_alias_binds_current_generation_and_is_consumed_by_commit(
    tmp_path: Path,
) -> None:
    card_field = ProtectedFieldDescriptor(
        name="card_number",
        label="Fixture card number",
        mode="stored",
        compatible_controls=("card_number",),
    )
    card_target = BackendTargetDescriptor(
        ref="e1",
        role="textbox",
        name="Card number",
        control_kind="text",
        frame_origin=_SECURE_LOCAL_ORIGIN,
        editable=True,
        protected=True,
        protected_kind="card_number",
    )
    commit_target = BackendTargetDescriptor(
        ref="e2",
        role="button",
        name="Pay now",
        control_kind="button",
        frame_origin=_SECURE_LOCAL_ORIGIN,
        consequential=True,
    )
    page = FakeBrowserPage(
        url=_SECURE_LOCAL_ORIGIN + "/checkout",
        snapshot='- textbox "Card number" [ref=e1]\n- button "Pay now" [ref=e2]',
        targets=(card_target, commit_target),
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))
    service, _backend, settings = _service(tmp_path, backend)
    broker = await _protected_broker(tmp_path, settings)
    card_ref = ProfileResourceRef(profile="personal", name="fixture-card")
    await broker.create(
        profile=card_ref.profile,
        name=card_ref.name,
        kind="payment_card",
        label="Fixture card",
        description="Synthetic payment source.",
        fields=(card_field,),
        policy=ProtectedDestinationPolicy(
            mode="strict",
            authored_origins=(_SECURE_LOCAL_ORIGIN,),
        ),
        values={"card_number": SecretStr("4242424242424242")},
    )
    opened = await service.open_session()
    snapshot = await service.snapshot(opened.session_id, page_id=None)
    fill_target = BrowserActionTarget.model_validate(
        snapshot.targets[0].model_dump(exclude={"navigation_generation"}),
        strict=True,
    )
    material = await broker.prepare(
        ProtectedUseRequest(
            ref=card_ref,
            field="card_number",
            consumer_id="browser.fill",
            control_kind="card_number",
            top_level_origin=_SECURE_LOCAL_ORIGIN,
            frame_origin=_SECURE_LOCAL_ORIGIN,
            occurrence=service.protected_occurrence(fill_target),
        )
    )

    filled = await service.protected_fill(fill_target, material, broker)
    assert filled.disposition == "performed"
    reviewed_snapshot = await service.snapshot(opened.session_id, page_id=None)
    submit = next(target for target in reviewed_snapshot.targets if target.ref == "e2")
    prepared = await service.prepare_commit(
        BrowserActionTarget.model_validate(
            submit.model_dump(exclude={"navigation_generation"}),
            strict=True,
        ),
        BrowserActionRequest(kind="commit", activation="click"),
    )
    assert prepared.payment_sources == (card_ref,)
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="financial",
        envelope_sha256="a" * 64,
    )

    committed = await service.commit_prepared(prepared, transaction)

    assert committed.disposition == "performed"
    assert committed.transaction == transaction
    assert committed.page.latest_action is not None
    assert committed.page.latest_action.transaction == transaction
    assert committed.snapshot is not None
    next_submit = next(target for target in committed.snapshot.targets if target.ref == "e2")
    next_prepared = await service.prepare_commit(
        BrowserActionTarget.model_validate(
            next_submit.model_dump(exclude={"navigation_generation"}),
            strict=True,
        ),
        BrowserActionRequest(kind="commit", activation="click"),
    )
    assert next_prepared.payment_sources == ()
    await broker.aclose()
    await service.aclose()


async def test_payment_card_alias_tracks_latest_field_fill_and_revalidates_before_commit(
    tmp_path: Path,
) -> None:
    card_field = ProtectedFieldDescriptor(
        name="card_number",
        label="Fixture card number",
        mode="stored",
        compatible_controls=("card_number",),
    )
    card_target = BackendTargetDescriptor(
        ref="e1",
        role="textbox",
        name="Card number",
        control_kind="text",
        frame_origin=_SECURE_LOCAL_ORIGIN,
        editable=True,
        protected=True,
        protected_kind="card_number",
    )
    commit_target = BackendTargetDescriptor(
        ref="e2",
        role="button",
        name="Pay now",
        control_kind="button",
        frame_origin=_SECURE_LOCAL_ORIGIN,
        consequential=True,
    )
    page = FakeBrowserPage(
        url=f"{_SECURE_LOCAL_ORIGIN}/checkout",
        snapshot='- textbox "Card number" [ref=e1]\n- button "Pay now" [ref=e2]',
        targets=(card_target, commit_target),
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))
    service, _backend, settings = _service(tmp_path, backend)
    broker = await _protected_broker(tmp_path, settings)
    first_ref = ProfileResourceRef(profile="personal", name="first-card")
    second_ref = ProfileResourceRef(profile="personal", name="second-card")
    for ref, number in (
        (first_ref, "4242424242424242"),
        (second_ref, "5555555555554444"),
    ):
        await broker.create(
            profile=ref.profile,
            name=ref.name,
            kind="payment_card",
            label=ref.name,
            description="Synthetic payment source.",
            fields=(card_field,),
            policy=ProtectedDestinationPolicy(
                mode="strict",
                authored_origins=(_SECURE_LOCAL_ORIGIN,),
            ),
            values={"card_number": SecretStr(number)},
        )

    opened = await service.open_session()

    async def fill(ref: ProfileResourceRef) -> None:
        snapshot = await service.snapshot(opened.session_id, page_id=None)
        target = next(item for item in snapshot.targets if item.ref == "e1")
        action_target = BrowserActionTarget.model_validate(
            target.model_dump(exclude={"navigation_generation"}),
            strict=True,
        )
        material = await broker.prepare(
            ProtectedUseRequest(
                ref=ref,
                field="card_number",
                consumer_id="browser.fill",
                control_kind="card_number",
                top_level_origin=_SECURE_LOCAL_ORIGIN,
                frame_origin=_SECURE_LOCAL_ORIGIN,
                occurrence=service.protected_occurrence(action_target),
            )
        )
        result = await service.protected_fill(action_target, material, broker)
        assert result.disposition == "performed"

    await fill(first_ref)
    await fill(second_ref)
    snapshot = await service.snapshot(opened.session_id, page_id=None)
    submit = next(item for item in snapshot.targets if item.ref == "e2")
    target = BrowserActionTarget.model_validate(
        submit.model_dump(exclude={"navigation_generation"}),
        strict=True,
    )
    prepared = await service.prepare_commit(
        target,
        BrowserActionRequest(kind="commit", activation="click"),
    )

    assert prepared.payment_sources == (second_ref,)
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="financial",
        envelope_sha256="b" * 64,
    )
    result = await service.action(
        target,
        prepared.request,
        expected_preflight=prepared.preflight,
        expected_payment_sources=(first_ref,),
        transaction=transaction,
    )
    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "stale_target"
    assert page.actions == []

    await broker.aclose()
    await service.aclose()


@pytest.mark.asyncio
async def test_cancelled_protected_dispatch_is_recorded_in_doubt_without_replay(
    tmp_path: Path,
) -> None:
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="textbox",
        name="Password",
        control_kind="text",
        frame_origin=_SECURE_LOCAL_ORIGIN,
        editable=True,
        protected=True,
        protected_kind="password",
    )
    page = FakeBrowserPage(
        url=f"{_SECURE_LOCAL_ORIGIN}/login",
        snapshot='- textbox "Password" [ref=e1]',
        targets=(descriptor,),
    )
    page.protected_fill_entered = asyncio.Event()
    page.protected_fill_release = asyncio.Event()
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))
    service, _backend, settings = _service(tmp_path, backend)
    broker = await _protected_broker(tmp_path, settings)
    opened = await service.open_session()
    snapshot = await service.snapshot(opened.session_id, page_id=None)
    target = BrowserActionTarget.model_validate(
        snapshot.targets[0].model_dump(exclude={"navigation_generation"}), strict=True
    )
    material = await broker.prepare(
        ProtectedUseRequest(
            ref=ProfileResourceRef(profile="personal", name="fixture-login"),
            field="password",
            consumer_id="browser.fill",
            control_kind="password",
            top_level_origin=_SECURE_LOCAL_ORIGIN,
            frame_origin=_SECURE_LOCAL_ORIGIN,
            occurrence=service.protected_occurrence(target),
        )
    )

    filling = asyncio.create_task(service.protected_fill(target, material, broker))
    await asyncio.wait_for(page.protected_fill_entered.wait(), timeout=2)
    filling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await filling

    [current] = (await service.pages(opened.session_id)).pages
    assert len(page.protected_fills) == 1
    assert current.latest_action is not None
    assert current.latest_action.kind == "protected_fill"
    assert current.latest_action.disposition == "in_doubt"
    assert current.latest_action.protected_ref == material.descriptor.ref
    assert current.latest_action.protected_field == "password"
    with pytest.raises(BrowserError, match="stale"):
        service.action_context(target)
    await broker.aclose()
    await service.aclose()


@pytest.mark.asyncio
async def test_protected_fill_revalidates_resource_policy_before_dispatch(
    tmp_path: Path,
) -> None:
    target_descriptor = BackendTargetDescriptor(
        ref="e1",
        role="textbox",
        name="Password",
        control_kind="text",
        frame_origin=_SECURE_LOCAL_ORIGIN,
        editable=True,
        protected=True,
        protected_kind="password",
    )
    page = FakeBrowserPage(
        url=f"{_SECURE_LOCAL_ORIGIN}/login",
        snapshot='- textbox "Password" [ref=e1]',
        targets=(target_descriptor,),
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))
    service, _backend, settings = _service(tmp_path, backend)
    broker = await _protected_broker(tmp_path, settings)
    opened = await service.open_session()
    snapshot = await service.snapshot(opened.session_id, page_id=None)
    target = BrowserActionTarget.model_validate(
        snapshot.targets[0].model_dump(exclude={"navigation_generation"}), strict=True
    )
    ref = ProfileResourceRef(profile="personal", name="fixture-login")
    material = await broker.prepare(
        ProtectedUseRequest(
            ref=ref,
            field="password",
            consumer_id="browser.fill",
            control_kind="password",
            top_level_origin=_SECURE_LOCAL_ORIGIN,
            frame_origin=_SECURE_LOCAL_ORIGIN,
            occurrence=service.protected_occurrence(target),
        )
    )
    current = (await broker.catalog(ref=ref))[0]
    await broker.revise(
        current,
        policy=ProtectedDestinationPolicy(mode="strict", authored_origins=("https://example.com",)),
    )

    result = await service.protected_fill(target, material, broker)

    assert result.disposition == "not_performed"
    assert result.failure is not None
    assert result.failure.code == "protected_field"
    assert page.protected_fills == []
    await broker.aclose()
    await service.aclose()


def _service(
    tmp_path: Path,
    backend: FakeBrowserBackend | None = None,
    **settings_overrides: int,
) -> tuple[BrowserService, FakeBrowserBackend, RickySettings]:
    selected_backend = backend or FakeBrowserBackend()
    settings = _settings(tmp_path, **settings_overrides)
    service = BrowserService(
        settings,
        scope=settings.resolve_profile_scope(),
        backend=selected_backend,
        executable_path=fake_executable(tmp_path),
    )
    return service, selected_backend, settings


async def test_owned_session_state_is_profile_scoped_and_cleaned(
    tmp_path: Path,
) -> None:
    service, backend, settings = _service(tmp_path)

    session = await service.open_session()
    [options] = backend.options
    assert isinstance(options, BrowserLaunchOptions)
    expected_profile_root = Path(settings.user_data_dir) / "profiles" / "personal"

    assert session.resource.profile == "personal"
    assert session.headless
    assert options.user_data_dir.is_relative_to(expected_profile_root)
    assert options.user_data_dir.is_dir()
    assert not Path(settings.project_data_dir).exists()

    await service.close_session(session.session_id)

    assert backend.sessions[0].closed
    assert not options.user_data_dir.exists()
    assert not Path(settings.project_data_dir).exists()
    await service.aclose()
    assert backend.closed


async def test_failed_launch_removes_partial_session_state(tmp_path: Path) -> None:
    backend = FakeBrowserBackend()
    backend.open_error = RuntimeError("launch failed")
    service, _, settings = _service(tmp_path, backend)

    with pytest.raises(RuntimeError, match="launch failed"):
        await service.open_session()

    ephemeral_root = (
        Path(settings.user_data_dir) / "profiles" / "personal" / settings.browser.ephemeral_dir
    )
    assert list(ephemeral_root.glob("runtime_*/browser_session_*")) == []
    await service.aclose()


async def test_initial_page_sync_failure_rolls_back_registered_session_and_state(
    tmp_path: Path,
) -> None:
    class FailingInitialSync(FakeBrowserSession):
        async def pages(self):
            raise RuntimeError("initial page sync failed")

    backend = FakeBrowserBackend()
    failed = FailingInitialSync()
    backend.pending_sessions.append(failed)
    service, _, settings = _service(tmp_path, backend)

    with pytest.raises(RuntimeError, match="initial page sync failed"):
        await service.open_session()

    assert failed.closed
    ephemeral_root = (
        Path(settings.user_data_dir) / "profiles" / "personal" / settings.browser.ephemeral_dir
    )
    assert list(ephemeral_root.glob("runtime_*/browser_session_*")) == []

    reopened = await service.open_session()
    assert reopened.session_id
    await service.aclose()


async def test_cancelled_initial_page_sync_rolls_back_registered_session_and_state(
    tmp_path: Path,
) -> None:
    class BlockingInitialSync(FakeBrowserSession):
        def __init__(self) -> None:
            super().__init__()
            self.pages_entered = asyncio.Event()

        async def pages(self) -> tuple[FakeBrowserPage, ...]:
            self.pages_entered.set()
            await asyncio.Future[None]()
            raise AssertionError("blocking initial sync unexpectedly resumed")

    backend = FakeBrowserBackend()
    blocked = BlockingInitialSync()
    backend.pending_sessions.append(blocked)
    service, _, settings = _service(tmp_path, backend)
    opening = asyncio.create_task(service.open_session())
    await asyncio.wait_for(blocked.pages_entered.wait(), timeout=2)

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    assert blocked.closed
    ephemeral_root = (
        Path(settings.user_data_dir) / "profiles" / "personal" / settings.browser.ephemeral_dir
    )
    assert list(ephemeral_root.glob("runtime_*/browser_session_*")) == []

    reopened = await service.open_session()
    assert reopened.session_id
    await service.aclose()


def test_ephemeral_state_rejects_existing_symlink_escape(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profile_root = Path(settings.user_data_dir) / "profiles" / "personal"
    profile_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (profile_root / "browser").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes profile data directory"):
        BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=FakeBrowserBackend(),
            executable_path=fake_executable(tmp_path),
        )

    assert list(outside.iterdir()) == []
    assert not Path(settings.project_data_dir).exists()


async def test_ephemeral_state_revalidates_symlinks_before_session_creation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    backend = FakeBrowserBackend()
    service = BrowserService(
        settings,
        scope=settings.resolve_profile_scope(),
        backend=backend,
        executable_path=fake_executable(tmp_path),
    )
    profile_root = Path(settings.user_data_dir) / "profiles" / "personal"
    profile_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "must-remain"
    marker.touch()
    (profile_root / "browser").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes profile data directory"):
        await service.open_session()

    assert backend.sessions == []
    await service.aclose()
    assert marker.is_file()
    assert not Path(settings.project_data_dir).exists()


async def test_session_limit_close_invalidation_and_idempotent_runtime_close(
    tmp_path: Path,
) -> None:
    service, backend, _settings_value = _service(tmp_path)
    session = await service.open_session()

    with pytest.raises(BrowserError) as limit:
        await service.open_session()
    assert limit.value.failure.code == "session_limit"

    await service.close_session(session.session_id)
    with pytest.raises(BrowserError) as stale:
        await service.pages(session.session_id)
    assert stale.value.failure.code == "unknown_session"

    await service.aclose()
    await service.aclose()
    assert backend.close_calls == 1


async def test_page_discovery_selection_limit_and_stale_page_ids(tmp_path: Path) -> None:
    service, backend, _settings_value = _service(tmp_path, max_pages=2)
    opened = await service.open_session()
    handle = backend.sessions[0]
    second = FakeBrowserPage("page-2", url="https://example.com/two", title="Two")
    overflow = FakeBrowserPage("page-3", url="https://example.com/three", title="Three")
    handle.page_handles.extend([second, overflow])

    pages = await service.pages(opened.session_id)

    assert len(pages.pages) == 2
    assert overflow.closed
    second_page = next(page for page in pages.pages if page.title == "Two")
    selected = await service.select_page(opened.session_id, second_page.page_id)
    assert selected.selected
    assert second.front_calls == 1

    with pytest.raises(BrowserError) as unknown:
        await service.select_page(opened.session_id, "browser_page_" + "f" * 32)
    assert unknown.value.failure.code == "unknown_page"
    await service.aclose()


async def test_page_observation_rejects_an_out_of_policy_live_url(tmp_path: Path) -> None:
    service, backend, _settings_value = _service(tmp_path)
    opened = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.url = "data:text/html,blocked"
    page.title = "must not be disclosed"

    with pytest.raises(BrowserError) as rejected:
        await service.pages(opened.session_id)

    assert rejected.value.failure.code == "invalid_destination"
    assert "must not be disclosed" not in rejected.value.failure.message
    await service.aclose()


async def test_navigation_projection_generation_scroll_and_snapshot_bounds(
    tmp_path: Path,
) -> None:
    service, backend, _settings_value = _service(tmp_path)
    opened = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.snapshot_text = (
        '- textbox "Card" [value="4111111111111111"]\n'
        '- link "Continue" [ref=e1]\n'
        '- button "More" [ref=e2]\n' + "x" * 1_100
    )

    navigation = await service.navigate(
        opened.session_id,
        page_id=None,
        url=f"{_LOCAL_ORIGIN}/checkout?token=secret&source=private#card",
    )
    scrolled = await service.scroll(
        opened.session_id,
        page_id=None,
        direction="up",
        amount=125,
    )
    snapshot = await service.snapshot(opened.session_id, page_id=None)

    assert navigation.page.navigation_generation == 1
    assert navigation.page.url == (f"{_LOCAL_ORIGIN}/checkout?token=redacted&source=present")
    assert "secret" not in navigation.page.url
    assert "private" not in navigation.page.url
    assert page.navigations == [f"{_LOCAL_ORIGIN}/checkout?token=secret&source=private"]
    assert scrolled.direction == "up"
    assert page.scrolls == [-125]
    assert snapshot.character_truncated
    assert len(snapshot.content) == 1_000
    assert "4111111111111111" not in snapshot.content
    assert [target.ref for target in snapshot.targets] == ["e1", "e2"]
    assert all(target.snapshot_id == snapshot.snapshot_id for target in snapshot.targets)
    assert all(
        target.navigation_generation == navigation.page.navigation_generation
        for target in snapshot.targets
    )
    assert page.snapshot_depths == [7]
    assert page.snapshot_character_limits == [1_000]
    await service.aclose()


async def test_snapshot_suppresses_nested_content_for_backend_protected_targets(
    tmp_path: Path,
) -> None:
    service, backend, _settings_value = _service(tmp_path)
    opened = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.snapshot_text = (
        '- generic "Credential editor" [ref=e1] [contenteditable=true]\n'
        "  - paragraph [ref=e2]\n"
        "    - text: nested-user-entered-secret\n"
        '- heading "Public status" [ref=e3]\n'
    )
    page.targets = (
        BackendTargetDescriptor(
            ref="e1",
            role="generic",
            name="Credential editor",
            control_kind="contenteditable",
            editable=True,
            protected=True,
        ),
        BackendTargetDescriptor(ref="e3", role="heading", name="Public status"),
    )

    snapshot = await service.snapshot(opened.session_id, page_id=None)
    serialized = snapshot.model_dump_json()

    assert "nested-user-entered-secret" not in serialized
    assert 'generic \\"Credential editor\\" [ref=e1]' in serialized
    assert "[contenteditable=true]" not in serialized
    assert "Public status" in serialized
    await service.aclose()


async def test_page_operations_are_fifo_and_never_replayed(tmp_path: Path) -> None:
    service, backend, _settings_value = _service(tmp_path)
    opened = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.navigate_entered = asyncio.Event()
    page.navigate_release = asyncio.Event()

    navigating = asyncio.create_task(
        service.navigate(opened.session_id, page_id=None, url=f"{_LOCAL_ORIGIN}/slow")
    )
    await asyncio.wait_for(page.navigate_entered.wait(), timeout=2)
    scrolling = asyncio.create_task(
        service.scroll(
            opened.session_id,
            page_id=None,
            direction="down",
            amount=200,
        )
    )
    await asyncio.sleep(0)
    assert page.operations == ["navigate:start"]

    page.navigate_release.set()
    await asyncio.wait_for(asyncio.gather(navigating, scrolling), timeout=2)

    assert page.operations == ["navigate:start", "navigate:end", "scroll"]
    assert page.navigations == [f"{_LOCAL_ORIGIN}/slow"]
    await service.aclose()


async def test_cancelled_navigation_releases_page_lock_without_retry(tmp_path: Path) -> None:
    service, backend, _settings_value = _service(tmp_path)
    opened = await service.open_session()
    page = backend.sessions[0].page_handles[0]
    page.snapshot_text = '- button "Continue" [ref=e1]'
    page.targets = (BackendTargetDescriptor(ref="e1", role="button", name="Continue"),)
    snapshot = await service.snapshot(opened.session_id, page_id=None)
    target = BrowserActionTarget(
        session_id=opened.session_id,
        page_id=snapshot.page.page_id,
        snapshot_id=snapshot.snapshot_id,
        ref="e1",
    )
    generation_before = snapshot.page.navigation_generation
    page.navigate_entered = asyncio.Event()
    page.navigate_release = asyncio.Event()
    navigating = asyncio.create_task(
        service.navigate(opened.session_id, page_id=None, url=f"{_LOCAL_ORIGIN}/cancel")
    )
    await asyncio.wait_for(page.navigate_entered.wait(), timeout=2)

    navigating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await navigating

    with pytest.raises(BrowserError) as stale:
        service.action_context(target)
    assert stale.value.failure.code == "stale_target"

    scrolled = await asyncio.wait_for(
        service.scroll(opened.session_id, page_id=None, direction="down", amount=10),
        timeout=2,
    )
    assert scrolled.page.navigation_generation == generation_before + 1
    assert page.navigations == [f"{_LOCAL_ORIGIN}/cancel"]
    assert page.scrolls == [10]
    await service.aclose()


async def test_failed_navigation_invalidates_snapshot_before_backend_error(
    tmp_path: Path,
) -> None:
    class FailingNavigationPage(FakeBrowserPage):
        async def navigate(self, url: str):
            self.navigations.append(url)
            raise RuntimeError("navigation failed after dispatch")

    page = FailingNavigationPage(
        snapshot='- button "Continue" [ref=e1]',
        targets=(BackendTargetDescriptor(ref="e1", role="button", name="Continue"),),
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))
    service, _, _settings_value = _service(tmp_path, backend)
    opened = await service.open_session()
    snapshot = await service.snapshot(opened.session_id, page_id=None)
    target = BrowserActionTarget(
        session_id=opened.session_id,
        page_id=snapshot.page.page_id,
        snapshot_id=snapshot.snapshot_id,
        ref="e1",
    )

    with pytest.raises(RuntimeError, match="navigation failed after dispatch"):
        await service.navigate(
            opened.session_id,
            page_id=None,
            url=f"{_LOCAL_ORIGIN}/failed",
        )

    with pytest.raises(BrowserError) as stale:
        service.action_context(target)
    assert stale.value.failure.code == "stale_target"
    pages = await service.pages(opened.session_id)
    assert pages.pages[0].navigation_generation == snapshot.page.navigation_generation + 1
    await service.aclose()


async def test_runtime_close_finishes_cleanup_when_caller_is_cancelled(tmp_path: Path) -> None:
    backend = FakeBrowserBackend()
    backend.close_entered = asyncio.Event()
    backend.close_release = asyncio.Event()
    service, _, _settings_value = _service(tmp_path, backend)
    await service.open_session()
    [options] = backend.options
    assert isinstance(options, BrowserLaunchOptions)
    closing = asyncio.create_task(service.aclose())
    await asyncio.wait_for(backend.close_entered.wait(), timeout=2)

    closing.cancel()
    backend.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert backend.closed
    assert not options.user_data_dir.exists()
