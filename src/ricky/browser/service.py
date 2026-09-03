"""Profile-scoped browser session ownership and coordination."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from ricky.attachments import BrowserDownloadRef, LoadedAttachment, browser_download_path
from ricky.browser.backend import (
    BackendActionOutcome,
    BackendActionPreflight,
    BackendActionRequest,
    BackendCoordinatePreflight,
    BackendCoordinateRequest,
    BackendPageState,
    BackendProtectedFillRequest,
    BackendTargetDescriptor,
    BackendUploadFile,
    BackendViewport,
    BrowserBackend,
    BrowserCdpOptions,
    BrowserLaunchOptions,
    BrowserPageHandle,
    BrowserSessionHandle,
)
from ricky.browser.install import browser_binary_dir, browser_status
from ricky.browser.lease import BrowserResourceLease
from ricky.browser.playwright_backend import PlaywrightBrowserBackend
from ricky.browser.policy import (
    DestinationPolicy,
    ValidatedDestination,
    canonical_origin,
    provider_safe_url,
    sanitize_aria_snapshot,
)
from ricky.browser.resources import (
    ResolvedBrowserResource,
    browser_resource_configuration_digest,
    persistent_browser_path,
    require_browser_resource,
    resolve_browser_resources,
)
from ricky.browser.runtime_guard import (
    BrowserBudgetKind,
    BrowserExecutionGuard,
    BrowserGuardFacts,
    BrowserRuntimeEvidence,
    BrowserToolName,
)
from ricky.browser.types import (
    BrowserActionContext,
    BrowserActionEvidence,
    BrowserActionKind,
    BrowserActionRequest,
    BrowserActionResult,
    BrowserActionTarget,
    BrowserAttachmentUseBinding,
    BrowserBoundingBox,
    BrowserCoordinateContext,
    BrowserCoordinateTarget,
    BrowserDialogPolicy,
    BrowserDownloadResult,
    BrowserError,
    BrowserFailure,
    BrowserHandoff,
    BrowserHandoffReason,
    BrowserNavigation,
    BrowserPage,
    BrowserPageChanges,
    BrowserPageList,
    BrowserPostcondition,
    BrowserProtectedUseBinding,
    BrowserResource,
    BrowserResourceList,
    BrowserResourceReset,
    BrowserScroll,
    BrowserSession,
    BrowserSessionClosed,
    BrowserSnapshot,
    BrowserTarget,
    BrowserTransactionEvidence,
    BrowserViewport,
    BrowserVisualCandidate,
    CoordinateFallbackEvidence,
)
from ricky.browser.visual import ComposedVisual, compose_numbered_visual
from ricky.config import (
    PersistentBrowserResourceSettings,
    RickySettings,
    ensure_private_user_data_root,
    profile_data_path,
    profile_data_subpath,
)
from ricky.profiles import ProfileName, ProfileResourceRef, ProfileScope
from ricky.protected_values import (
    ProtectedMaterial,
    ProtectedValueBroker,
    ProtectedValueKind,
)

_REF = re.compile(r"\[ref=((?:f[0-9]+)?e[0-9]+)\]")
_ACTION_TO_TOOL: dict[BrowserActionKind, BrowserToolName] = {
    "click": "browser_click",
    "fill": "browser_fill",
    "select": "browser_select",
    "set_checked": "browser_set_checked",
    "press_key": "browser_press_key",
    "commit": "browser_commit",
    "upload": "browser_upload",
    "download": "browser_download",
    "coordinate_click": "browser_coordinate_click",
    "coordinate_commit": "browser_coordinate_commit",
    "protected_fill": "browser_fill_protected",
}


class _DownloadPublicationTooLarge(ValueError):
    """A completed download exceeded its hard durable-publication ceiling."""


async def _join_download_publication(
    operation: asyncio.Task[BrowserDownloadRef],
) -> tuple[BrowserDownloadRef | None, Exception | None, bool]:
    """Join a non-cancellable worker and report deferred caller cancellation."""

    interrupted = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    try:
        return operation.result(), None, interrupted
    except Exception as exc:
        return None, exc, interrupted


@dataclass
class _PageEntry:
    id: str
    handle: BrowserPageHandle
    exact_url: str
    title: str = ""
    generation: int = 0
    snapshot_id: str | None = None
    targets_by_ref: dict[str, tuple[BackendTargetDescriptor, ...]] = field(default_factory=dict)
    latest_action: BrowserActionEvidence | None = None
    protected_uses: list[_ProtectedPageUse] = field(default_factory=list)
    uploaded_attachments: dict[str, _UploadedPageUse] = field(default_factory=dict)
    uncertain_attachment_targets: dict[str, int] = field(default_factory=dict)
    visual: _VisualEntry | None = None
    semantic: _SemanticEntry | None = None
    semantic_preflight_failure: _SemanticPreflightFailure | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _SessionEntry:
    id: str
    resource: ProfileResourceRef
    mode: Literal["owned_ephemeral", "owned_persistent", "attached_cdp"]
    headless: bool | None
    resource_configuration_digest: str | None
    state_dir: Path | None
    download_temp_dir: Path | None
    handle: BrowserSessionHandle
    lease: BrowserResourceLease | None = None
    pages_by_key: dict[str, _PageEntry] = field(default_factory=dict)
    selected_page_id: str | None = None
    closing: bool = False
    cleanup_failed: bool = False
    guarded_controlled_pages: int = 0
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    action_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class _PageSyncResult:
    discarded_page_ids: tuple[str, ...] = ()
    closed_page_ids: tuple[str, ...] = ()
    blocked_page_ids: tuple[str, ...] = ()
    discarded_page_count: int = 0
    unsupported_page_count: int = 0
    blocked_page_count: int = 0


@dataclass(frozen=True)
class _VisualEntry:
    snapshot_id: str
    navigation_generation: int
    masked_base_sha256: str
    viewport: BackendViewport
    composed: ComposedVisual
    admitted_provider: str | None = None


@dataclass(frozen=True)
class _SemanticEntry:
    snapshot_id: str
    navigation_generation: int
    targets_by_ref: dict[str, tuple[BackendTargetDescriptor, ...]]


@dataclass(frozen=True)
class _SemanticPreflightFailure:
    target: BrowserActionTarget
    failure_code: str
    action_kind: Literal["click", "commit"]


@dataclass(frozen=True)
class _ProtectedPageUse:
    ref: ProfileResourceRef
    kind: ProtectedValueKind
    generation: int
    frame_key: str
    target_ref: str
    field: str
    revision: int


@dataclass(frozen=True)
class _UploadedPageUse:
    generation: int
    target_ref: str
    attachments: tuple[BrowserAttachmentUseBinding, ...]


@dataclass(frozen=True, repr=False)
class BrowserPreparedCommit:
    """Exact semantic commit facts frozen before foreground review."""

    target: BrowserActionTarget
    request: BrowserActionRequest
    context: BrowserActionContext
    preflight: BackendActionPreflight
    payment_sources: tuple[ProfileResourceRef, ...]
    protected_uses: tuple[BrowserProtectedUseBinding, ...] = ()
    attachments: tuple[BrowserAttachmentUseBinding, ...] = ()

    @property
    def financial_signal(self) -> bool:
        return self.preflight.financial_signal or bool(self.payment_sources)


@dataclass(frozen=True, repr=False)
class BrowserPreparedCoordinateCommit:
    """Exact visual commit facts frozen before foreground review."""

    target: BrowserCoordinateTarget
    dialog: BrowserDialogPolicy
    context: BrowserCoordinateContext
    viewport: BackendViewport
    preflight: BackendCoordinatePreflight
    payment_sources: tuple[ProfileResourceRef, ...]
    protected_uses: tuple[BrowserProtectedUseBinding, ...] = ()
    attachments: tuple[BrowserAttachmentUseBinding, ...] = ()
    fallback: CoordinateFallbackEvidence | None = None

    @property
    def financial_signal(self) -> bool:
        return self.preflight.financial_signal or bool(self.payment_sources)


@dataclass(frozen=True, repr=False)
class BrowserPreparedCoordinateClick:
    """Exact semantic-first visual click facts frozen before dispatch."""

    target: BrowserCoordinateTarget
    context: BrowserCoordinateContext
    viewport: BackendViewport
    preflight: BackendCoordinatePreflight
    fallback: CoordinateFallbackEvidence


@dataclass(frozen=True)
class BrowserVisualCapture:
    """In-process screenshot bytes plus provider-safe visual metadata."""

    snapshot_id: str
    page: BrowserPage
    png: bytes
    width: int
    height: int
    viewport: BrowserViewport
    candidates: tuple[BrowserVisualCandidate, ...]
    candidate_truncated: bool
    masked_base_sha256: str


class BrowserService:
    """Own profile-scoped browser resources and expose Ricky domain values."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        scope: ProfileScope,
        backend: BrowserBackend,
        executable_path: Path,
        runtime_guard: BrowserExecutionGuard | None = None,
    ) -> None:
        self._settings = settings
        self._scope = scope
        self._backend = backend
        self._executable_path = executable_path
        self._runtime_guard = runtime_guard
        allowed_private_origins = set(settings.browser.allowed_private_origins)
        if runtime_guard is not None:
            allowed_private_origins &= set(runtime_guard.private_origin_ceiling)
        self._policy = DestinationPolicy(
            allowed_private_origins=allowed_private_origins,
            resolution_timeout_seconds=settings.browser.operation_timeout_seconds,
        )
        self._validated_private_origins: set[str] = set()
        self._instance_id = f"runtime_{uuid.uuid4().hex}"
        self._instance_dir = self._confined_state_path(self._instance_id)
        self._instance_state_created = False
        self._sessions: dict[str, _SessionEntry] = {}
        self._registry_lock = asyncio.Lock()
        self._closed = False
        self._close_complete = False

    @classmethod
    async def create(
        cls,
        settings: RickySettings,
        *,
        scope: ProfileScope,
        backend: BrowserBackend | None = None,
        runtime_guard: BrowserExecutionGuard | None = None,
    ) -> BrowserService:
        """Construct without starting Chromium or creating profile state."""

        status = await browser_status(settings)
        executable = Path(status.executable) if status.executable else browser_binary_dir(settings)
        selected_backend = backend or PlaywrightBrowserBackend()
        return cls(
            settings,
            scope=scope,
            backend=selected_backend,
            executable_path=executable,
            runtime_guard=runtime_guard,
        )

    @property
    def background_guarded(self) -> bool:
        """Whether this service was constructed behind an execution-owned live guard."""

        return self._runtime_guard is not None

    @property
    def execution_id(self) -> str | None:
        """Return the owning execution id only for a guarded background runtime."""

        return None if self._runtime_guard is None else self._runtime_guard.execution_id

    def bind_effect_action(self, action_id: str, action_key: str) -> None:
        """Correlate the next guarded browser effect with the shared action ledger."""

        if self._runtime_guard is not None:
            self._runtime_guard.bind_effect_action(action_id, action_key)

    async def settle_effect_action(self, action_id: str) -> None:
        """Publish correlated browser evidence after shared-action finalization."""

        if self._runtime_guard is not None:
            await self._runtime_guard.settle_effect_action(action_id)

    async def open_session(self, *, headless: bool | None = None) -> BrowserSession:
        async with self._registry_lock:
            self._ensure_open()
            if len(self._sessions) >= self._settings.browser.max_sessions:
                raise BrowserError(
                    BrowserFailure(
                        code="session_limit",
                        message="the configured live browser session limit has been reached",
                    )
                )
            session_id = f"browser_session_{uuid.uuid4().hex}"
            ephemeral_resource = ProfileResourceRef(
                profile=self._scope.primary,
                name=session_id,
            )
            selected_headless = self._settings.browser.headless if headless is None else headless
            if self._runtime_guard is not None and selected_headless is not True:
                raise BrowserError(
                    BrowserFailure(
                        code="unattended_denied",
                        message="background browser sessions must use owned headless Chromium",
                    )
                )
            guard_facts = self._guard_facts(
                "browser_session_open",
                resource=ephemeral_resource,
                session_id=session_id,
                session_mode="owned_ephemeral",
                headless=selected_headless,
            )
            await self._reserve_guard("session_starts", 1, guard_facts)
            state_dir = self._confined_state_path(self._instance_id, session_id)
            if state_dir.parent != self._instance_dir:
                raise ValueError("browser ephemeral state path changed after runtime creation")
            ensure_private_user_data_root(self._settings)
            self._instance_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._instance_state_created = True
            state_dir.mkdir(exist_ok=False, mode=0o700)
            download_temp_dir = state_dir / "download-attempts"
            download_temp_dir.mkdir(exist_ok=False, mode=0o700)
            if os.name == "posix":
                os.chmod(self._instance_dir, 0o700)
                os.chmod(state_dir, 0o700)
            options = BrowserLaunchOptions(
                mode="owned_ephemeral",
                user_data_dir=state_dir,
                download_temp_dir=download_temp_dir,
                executable_path=self._executable_path,
                headless=selected_headless,
                navigation_timeout_ms=self._settings.browser.navigation_timeout_seconds * 1_000,
                operation_timeout_ms=self._settings.browser.operation_timeout_seconds * 1_000,
                max_redirects=self._settings.browser.max_redirects,
                page_discovery_limit=(
                    self._runtime_guard.controlled_page_ceiling
                    if self._runtime_guard is not None
                    else self._settings.browser.max_pages + 50
                ),
                download_file_byte_limit=self._settings.browser.download_file_byte_limit,
            )
            guarded_pages = 1 if self._runtime_guard is not None else 0
            await self._reserve_guard("controlled_pages", guarded_pages, guard_facts)
            try:
                handle = await self._backend.open_session(
                    options,
                    destination_guard=self._guard_destination,
                )
            except BaseException:
                try:
                    await self._release_controlled_pages(guarded_pages, guard_facts)
                finally:
                    shutil.rmtree(state_dir, ignore_errors=True)
                raise
            entry = _SessionEntry(
                id=session_id,
                resource=ephemeral_resource,
                mode="owned_ephemeral",
                headless=selected_headless,
                resource_configuration_digest=None,
                state_dir=state_dir,
                download_temp_dir=download_temp_dir,
                handle=handle,
                guarded_controlled_pages=guarded_pages,
            )
            self._sessions[session_id] = entry
        try:
            await self._sync_pages(entry)
            result = await self._session_model(entry)
            await self._record_guard(guard_facts, disposition="completed")
            return result
        except BaseException:
            await self._rollback_open_session(entry)
            raise

    async def resources(self) -> BrowserResourceList:
        """List bounded safe metadata for resources inside the issued scope."""

        self._ensure_open()
        guard_facts = self._guard_facts("browser_resources")
        await self._check_guard(guard_facts)
        result = BrowserResourceList(
            resources=tuple(
                self._resource_model(resource)
                for resource in resolve_browser_resources(self._settings, scope=self._scope)
            )
        )
        await self._record_guard(guard_facts, disposition="completed")
        return result

    def resource(self, qualified: str) -> BrowserResource:
        """Resolve one provider-safe resource synchronously for permission review."""

        return self._resource_model(self._require_resource(qualified))

    async def open_resource(
        self,
        qualified: str,
        *,
        headless: bool | None = None,
        start_blank: bool = False,
    ) -> BrowserSession:
        """Open one exact configured persistent profile or CDP attachment."""

        resolved = self._require_resource(qualified)
        async with self._registry_lock:
            self._ensure_open()
            if len(self._sessions) >= self._settings.browser.max_sessions:
                raise BrowserError(
                    BrowserFailure(
                        code="session_limit",
                        message="the configured live browser session limit has been reached",
                    )
                )
            session_id = f"browser_session_{uuid.uuid4().hex}"
            session_mode: Literal["owned_persistent", "attached_cdp"] = (
                "owned_persistent"
                if isinstance(resolved.settings, PersistentBrowserResourceSettings)
                else "attached_cdp"
            )
            selected_guard_headless = (
                resolved.settings.headless
                if isinstance(resolved.settings, PersistentBrowserResourceSettings)
                and headless is None
                else headless
            )
            if self._runtime_guard is not None and (
                session_mode != "owned_persistent" or selected_guard_headless is not True
            ):
                raise BrowserError(
                    BrowserFailure(
                        code="unattended_denied",
                        message=(
                            "background browser resources must use Ricky-owned headless Chromium"
                        ),
                    )
                )
            guard_facts = self._guard_facts(
                "browser_session_open_resource",
                resource=resolved.ref,
                session_id=session_id,
                session_mode=session_mode,
                headless=selected_guard_headless,
                resource_configuration_digest=browser_resource_configuration_digest(resolved),
            )
            await self._reserve_guard("session_starts", 1, guard_facts)
            lease = BrowserResourceLease(self._settings, resolved.ref)
            lease.acquire()
            guarded_pages = 0
            state_dir: Path | None = None
            download_temp_dir: Path | None = None
            attachment_deadline: float | None = None
            try:
                if isinstance(resolved.settings, PersistentBrowserResourceSettings):
                    state_dir = self._prepare_persistent_state(resolved.ref)
                    download_temp_dir = self._confined_state_path(
                        self._instance_id, session_id, "download-attempts"
                    )
                    ensure_private_user_data_root(self._settings)
                    download_temp_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
                    self._instance_state_created = True
                    selected_headless = resolved.settings.headless if headless is None else headless
                    mode: Literal["owned_persistent", "attached_cdp"] = "owned_persistent"
                    options = BrowserLaunchOptions(
                        mode=mode,
                        user_data_dir=state_dir,
                        download_temp_dir=download_temp_dir,
                        executable_path=self._executable_path,
                        headless=selected_headless,
                        navigation_timeout_ms=(
                            self._settings.browser.navigation_timeout_seconds * 1_000
                        ),
                        operation_timeout_ms=(
                            self._settings.browser.operation_timeout_seconds * 1_000
                        ),
                        max_redirects=self._settings.browser.max_redirects,
                        page_discovery_limit=(
                            self._runtime_guard.controlled_page_ceiling
                            if self._runtime_guard is not None
                            else self._settings.browser.max_pages + 50
                        ),
                        download_file_byte_limit=(self._settings.browser.download_file_byte_limit),
                        start_blank=start_blank,
                    )
                else:
                    if headless is not None:
                        raise BrowserError(
                            BrowserFailure(
                                code="resource_kind_mismatch",
                                message=(
                                    "attached browser resources do not accept a headless override"
                                ),
                            )
                        )
                    if start_blank:
                        raise BrowserError(
                            BrowserFailure(
                                code="resource_kind_mismatch",
                                message="attached browser resources cannot replace external tabs",
                            )
                        )
                    selected_headless = None
                    mode = "attached_cdp"
                    attachment_deadline = (
                        asyncio.get_running_loop().time()
                        + self._settings.browser.attachment_timeout_seconds
                    )
                    options = BrowserCdpOptions(
                        mode=mode,
                        endpoint=resolved.settings.endpoint,
                        attachment_timeout_ms=(
                            self._settings.browser.attachment_timeout_seconds * 1_000
                        ),
                        navigation_timeout_ms=(
                            self._settings.browser.navigation_timeout_seconds * 1_000
                        ),
                        operation_timeout_ms=(
                            self._settings.browser.operation_timeout_seconds * 1_000
                        ),
                        max_redirects=self._settings.browser.max_redirects,
                        page_discovery_limit=self._settings.browser.max_pages + 50,
                    )
                guarded_pages = (
                    self._runtime_guard.controlled_page_ceiling
                    if self._runtime_guard is not None
                    else 0
                )
                await self._reserve_guard("controlled_pages", guarded_pages, guard_facts)
                if attachment_deadline is None:
                    handle = await self._backend.open_session(
                        options,
                        destination_guard=self._guard_destination,
                    )
                else:
                    async with asyncio.timeout_at(attachment_deadline):
                        handle = await self._backend.open_session(
                            options,
                            destination_guard=self._guard_destination,
                        )
            except TimeoutError as exc:
                try:
                    await self._release_controlled_pages(
                        guarded_pages,
                        guard_facts,
                    )
                finally:
                    if download_temp_dir is not None:
                        shutil.rmtree(download_temp_dir.parent, ignore_errors=True)
                    lease.release()
                raise _attachment_timeout() from exc
            except BaseException:
                try:
                    await self._release_controlled_pages(
                        guarded_pages,
                        guard_facts,
                    )
                finally:
                    if download_temp_dir is not None:
                        shutil.rmtree(download_temp_dir.parent, ignore_errors=True)
                    lease.release()
                raise
            entry = _SessionEntry(
                id=session_id,
                resource=resolved.ref,
                mode=mode,
                headless=selected_headless,
                resource_configuration_digest=browser_resource_configuration_digest(resolved),
                state_dir=state_dir,
                download_temp_dir=download_temp_dir,
                handle=handle,
                lease=lease,
                guarded_controlled_pages=guarded_pages,
            )
            self._sessions[session_id] = entry
        try:
            if attachment_deadline is None:
                result = await self._complete_resource_open(entry)
                await self._record_guard(guard_facts, disposition="completed")
                return result
            async with asyncio.timeout_at(attachment_deadline):
                result = await self._complete_resource_open(entry)
                await self._record_guard(guard_facts, disposition="completed")
                return result
        except TimeoutError as exc:
            await self._rollback_open_session(entry)
            raise _attachment_timeout() from exc
        except BaseException:
            await self._rollback_open_session(entry)
            raise

    async def _complete_resource_open(self, entry: _SessionEntry) -> BrowserSession:
        sync = await self._sync_pages(entry)
        if sync.discarded_page_count:
            raise BrowserError(
                BrowserFailure(
                    code="page_limit" if entry.handle.pages_owned else "attached_page_limit",
                    message=(
                        "configured browser profile exceeds the page limit"
                        if entry.handle.pages_owned
                        else "configured browser attachment exceeds the page limit"
                    ),
                )
            )
        if entry.selected_page_id is None:
            raise BrowserError(
                BrowserFailure(
                    code="unsupported_attached_page",
                    message="configured browser attachment has no supported Web page",
                )
            )
        return await self._session_model(entry)

    async def reset_resource(self, qualified: str) -> BrowserResourceReset:
        """Delete only one idle persistent profile directory."""

        resolved = self._require_resource(qualified)
        if not isinstance(resolved.settings, PersistentBrowserResourceSettings):
            raise BrowserError(
                BrowserFailure(
                    code="resource_kind_mismatch",
                    message="only persistent browser resources can be reset",
                )
            )
        lease = BrowserResourceLease(self._settings, resolved.ref)
        lease.acquire()
        try:
            path = persistent_browser_path(self._settings, resolved.ref)
            expected = persistent_browser_path(self._settings, resolved.ref)
            if path != expected:
                raise ValueError("persistent browser path changed before reset")
            if path.exists():
                shutil.rmtree(path, ignore_errors=False)
            _remove_empty_parents(
                path.parent,
                stop=profile_data_path(self._settings, resolved.ref.profile),
            )
        finally:
            lease.release()
        return BrowserResourceReset(resource=resolved.ref)

    async def close_session(self, session_id: str) -> BrowserSessionClosed:
        async with self._registry_lock:
            self._ensure_open()
            entry = self._sessions.get(session_id)
            if entry is None:
                raise BrowserError(
                    BrowserFailure(code="unknown_session", message="unknown browser session id")
                )
            if entry.closing:
                raise BrowserError(
                    BrowserFailure(code="session_closed", message="browser session is closing")
                )
            guard_facts = self._guard_facts("browser_session_close", entry=entry)
            await self._check_guard(guard_facts)
        close_task = asyncio.create_task(self._close_entry_when_idle(entry))
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await close_task
            raise
        async with self._registry_lock:
            if self._sessions.get(session_id) is entry:
                self._sessions.pop(session_id)
        result = BrowserSessionClosed(session_id=session_id)
        await self._record_guard(guard_facts, disposition="completed")
        return result

    async def pages(self, session_id: str) -> BrowserPageList:
        entry = self._require_session(session_id)
        guard_facts = self._guard_facts("browser_pages", entry=entry)
        await self._check_guard(guard_facts)
        await self._sync_pages(entry)
        page_models: list[BrowserPage] = []
        for page in entry.pages_by_key.values():
            async with page.lock:
                page_models.append(await self._page_model(entry, page))
        pages = tuple(page_models)
        selected = entry.selected_page_id
        if selected is None:
            raise BrowserError(
                BrowserFailure(code="session_closed", message="browser session has no open pages")
            )
        result = BrowserPageList(session_id=entry.id, selected_page_id=selected, pages=pages)
        await self._record_guard(guard_facts, disposition="completed")
        return result

    async def select_page(self, session_id: str, page_id: str) -> BrowserPage:
        entry, page = await self._require_page(session_id, page_id)
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            guard_facts = self._guard_facts("browser_page_select", entry=entry, page=page)
            await self._check_guard(guard_facts)
            await page.handle.bring_to_front()
            entry.selected_page_id = page.id
            result = await self._page_model(entry, page)
            await self._record_guard(guard_facts, disposition="completed")
            return result

    async def navigate(
        self,
        session_id: str,
        *,
        page_id: str | None,
        url: str,
    ) -> BrowserNavigation:
        validated = await self._validate_destination(url)
        entry, page = await self._require_page(session_id, page_id)
        async with page.lock:
            self._ensure_session_active(entry)
            guard_facts = self._guard_facts(
                "browser_navigate",
                entry=entry,
                page=page,
                effective_destinations=(validated.url,),
            )
            await self._reserve_guard("navigations", 1, guard_facts)
            page.generation += 1
            page.protected_uses.clear()
            self._invalidate_snapshot(page)
            try:
                state = await page.handle.navigate(validated.url)
            except asyncio.CancelledError:
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    failure=BrowserFailure(
                        code="action_in_doubt",
                        message="browser navigation was interrupted after it may have begun",
                        outcome_uncertain=True,
                    ),
                )
                raise
            except BrowserError as exc:
                await self._record_guard(
                    guard_facts,
                    disposition=("in_doubt" if exc.failure.outcome_uncertain else "not_performed"),
                    failure=exc.failure,
                )
                raise
            except Exception:
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    failure=BrowserFailure(
                        code="action_in_doubt",
                        message="browser navigation failed after it may have begun",
                        outcome_uncertain=True,
                    ),
                )
                raise
            page.exact_url = state.url
            page.title = state.title
            result = BrowserNavigation(page=await self._page_model(entry, page, state=state))
            await self._record_guard(guard_facts, disposition="completed")
            return result

    async def scroll(
        self,
        session_id: str,
        *,
        page_id: str | None,
        direction: Literal["up", "down"],
        amount: int,
    ) -> BrowserScroll:
        entry, page = await self._require_page(session_id, page_id)
        delta = amount if direction == "down" else -amount
        async with page.lock:
            self._ensure_session_active(entry)
            guard_facts = self._guard_facts("browser_scroll", entry=entry, page=page)
            await self._reserve_guard("scrolls", 1, guard_facts)
            try:
                state = await page.handle.scroll(delta)
            except asyncio.CancelledError:
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    failure=BrowserFailure(
                        code="action_in_doubt",
                        message="browser scroll was interrupted after it may have begun",
                        outcome_uncertain=True,
                    ),
                )
                raise
            except Exception:
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    failure=BrowserFailure(
                        code="action_in_doubt",
                        message="browser scroll failed after it may have begun",
                        outcome_uncertain=True,
                    ),
                )
                raise
            result = BrowserScroll(
                page=await self._page_model(entry, page, state=state),
                direction=direction,
                amount=amount,
            )
            await self._record_guard(guard_facts, disposition="completed")
            return result

    async def snapshot(
        self,
        session_id: str,
        *,
        page_id: str | None,
    ) -> BrowserSnapshot:
        entry, page = await self._require_page(session_id, page_id)
        async with page.lock:
            return await self._snapshot_locked(entry, page)

    def screenshot_source_owner(self, session_id: str) -> ProfileName:
        """Return the already-authorized browser resource owner for screenshot policy."""
        return self._require_session(session_id).resource.profile

    async def visual_snapshot(
        self,
        session_id: str,
        *,
        page_id: str | None,
        provider: str | None = None,
    ) -> BrowserVisualCapture:
        """Capture, compose, and cache one exact masked visual generation."""
        entry, page = await self._require_page(session_id, page_id)
        async with page.lock:
            self._ensure_session_active(entry)
            state_before = await page.handle.state()
            if state_before.closed:
                raise BrowserError(
                    BrowserFailure(code="page_closed", message="browser page is closed")
                )
            await self._page_model(entry, page, state=state_before)
            if self._runtime_guard is not None and provider is None:
                raise BrowserError(
                    BrowserFailure(
                        code="screenshot_denied",
                        message="background visual capture requires its pinned model provider",
                    )
                )
            if provider is not None:
                self._require_screenshot_disclosure(entry, provider)
            guard_facts = self._guard_facts(
                "browser_visual_snapshot",
                entry=entry,
                page=page,
                provider=provider,
            )
            await self._reserve_guard("visual_observations", 1, guard_facts)
            capture = await page.handle.visual_snapshot(
                candidate_limit=self._settings.browser.visual_candidate_limit
            )
            if len(capture.png) > self._settings.browser.screenshot_file_byte_limit:
                raise BrowserError(
                    BrowserFailure(
                        code="screenshot_too_large",
                        message="masked browser screenshot exceeds the configured byte limit",
                    )
                )
            if capture.viewport.width * capture.viewport.height > (
                self._settings.browser.screenshot_pixel_limit
            ):
                raise BrowserError(
                    BrowserFailure(
                        code="screenshot_too_large",
                        message="browser viewport exceeds the configured screenshot pixel limit",
                    )
                )
            composed = compose_numbered_visual(capture, self._settings.browser)
            state_after = await page.handle.state()
            if state_after.closed or state_after.url != state_before.url:
                if not state_after.closed:
                    page.exact_url = state_after.url
                    page.title = state_after.title
                    page.generation += 1
                    page.protected_uses.clear()
                self._invalidate_snapshot(page)
                raise BrowserError(
                    BrowserFailure(
                        code="backend_error",
                        message="browser page changed during visual capture; request another",
                        retryable=True,
                    )
                )
            snapshot_id = f"browser_snapshot_{uuid.uuid4().hex}"
            page.snapshot_id = snapshot_id
            page.targets_by_ref = {
                candidate.descriptor.ref: (candidate.descriptor,)
                for candidate in capture.candidates
            }
            page.visual = _VisualEntry(
                snapshot_id=snapshot_id,
                navigation_generation=page.generation,
                masked_base_sha256=capture.masked_base_sha256,
                viewport=capture.viewport,
                composed=composed,
                admitted_provider=provider,
            )
            model = await self._page_model(entry, page, state=state_after)
            viewport = BrowserViewport(
                width=capture.viewport.width,
                height=capture.viewport.height,
                scroll_x=capture.viewport.scroll_x,
                scroll_y=capture.viewport.scroll_y,
                device_scale_factor=capture.viewport.device_scale_factor,
                image_scale=composed.image_scale,
            )
            candidates = tuple(
                BrowserVisualCandidate(
                    number=number,
                    target=BrowserTarget(
                        ref=candidate.descriptor.ref,
                        session_id=entry.id,
                        page_id=page.id,
                        navigation_generation=page.generation,
                        snapshot_id=snapshot_id,
                    ),
                    descriptor=candidate.descriptor.provider_descriptor(),
                    bounding_box=BrowserBoundingBox(
                        x=candidate.bounding_box.x,
                        y=candidate.bounding_box.y,
                        width=candidate.bounding_box.width,
                        height=candidate.bounding_box.height,
                    ),
                )
                for number, candidate in enumerate(capture.candidates, start=1)
            )
            result = BrowserVisualCapture(
                snapshot_id=snapshot_id,
                page=model,
                png=composed.png,
                width=composed.width,
                height=composed.height,
                viewport=viewport,
                candidates=candidates,
                candidate_truncated=capture.candidate_truncated,
                masked_base_sha256=capture.masked_base_sha256,
            )
            await self._record_guard(guard_facts, disposition="completed")
            return result

    async def revalidate_visual_disclosure(
        self,
        session_id: str,
        page_id: str,
        snapshot_id: str,
        *,
        provider: str,
    ) -> None:
        """Recheck source-owner and execution disclosure immediately before encoding."""

        entry, page = await self._require_page(session_id, page_id)
        async with page.lock:
            self._ensure_session_active(entry)
            visual = page.visual
            if (
                visual is None
                or visual.snapshot_id != snapshot_id
                or visual.admitted_provider != provider
            ):
                raise BrowserError(
                    BrowserFailure(
                        code="screenshot_denied",
                        message="browser screenshot disclosure binding changed",
                    )
                )
            self._require_screenshot_disclosure(entry, provider)
            await self._check_guard(
                self._guard_facts(
                    "browser_visual_snapshot",
                    entry=entry,
                    page=page,
                    provider=provider,
                    snapshot_id=snapshot_id,
                )
            )

    def discard_visual_snapshot(self, session_id: str, page_id: str, snapshot_id: str) -> None:
        """Invalidate a captured generation whose media admission failed."""
        entry = self._require_session(session_id)
        page = self._page_entry(entry, page_id)
        if page.snapshot_id == snapshot_id:
            self._invalidate_snapshot(page)

    def action_context(self, target: BrowserActionTarget) -> BrowserActionContext:
        """Return cached safe facts without touching the live backend."""

        entry = self._require_session(target.session_id)
        page = self._page_entry(entry, target.page_id)
        descriptor = self._cached_target(page, target)
        origin = None
        if page.exact_url != "about:blank":
            try:
                origin = canonical_origin(page.exact_url)
            except ValueError:
                origin = None
        return BrowserActionContext(
            target=target,
            resource=entry.resource,
            navigation_generation=page.generation,
            url=provider_safe_url(page.exact_url),
            origin=origin,
            descriptor=descriptor.provider_descriptor(),
            headless=entry.headless,
        )

    async def prepare_commit(
        self,
        target: BrowserActionTarget,
        request: BrowserActionRequest,
    ) -> BrowserPreparedCommit:
        """Freeze one live semantic commit occurrence without dispatching it."""

        if request.kind != "commit":
            raise ValueError("prepared browser commit requires a commit action")
        entry, page = await self._require_page(target.session_id, target.page_id)
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            state = await page.handle.state()
            await self._page_model(entry, page, state=state)
            cached = self._cached_target(page, target)
            if failure := self._validate_action(cached, request):
                raise BrowserError(failure)
            context = self.action_context(target)
            await self._validate_transaction_origins(
                context.origin,
                cached.frame_origin,
            )
            guard_facts = self._guard_facts(
                "browser_commit",
                entry=entry,
                page=page,
                target_frame_origin=cached.frame_origin,
                snapshot_id=target.snapshot_id,
                target_ref=target.ref,
                action_kind="commit",
            )
            await self._check_guard(guard_facts)
            backend_request = BackendActionRequest(
                action_id=f"browser_action_{uuid.uuid4().hex}",
                action=request,
                target=cached,
            )
            try:
                preflight = await page.handle.preflight_action(backend_request)
            except BrowserError as exc:
                if self._runtime_guard is not None:
                    page.semantic_preflight_failure = _SemanticPreflightFailure(
                        target=target,
                        failure_code=exc.failure.code,
                        action_kind="commit",
                    )
                raise
            except Exception as exc:
                if self._runtime_guard is not None:
                    page.semantic_preflight_failure = _SemanticPreflightFailure(
                        target=target,
                        failure_code="backend_error",
                        action_kind="commit",
                    )
                raise BrowserError(
                    BrowserFailure(
                        code="backend_error",
                        message="browser transaction preflight failed before dispatch",
                    )
                ) from exc
            if preflight.target != cached:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="browser target changed while preparing transaction review",
                    )
                )
            await self._check_guard(
                self._guard_facts(
                    "browser_commit",
                    entry=entry,
                    page=page,
                    target_frame_origin=preflight.target.frame_origin,
                    effective_destinations=preflight.effective_destinations,
                    snapshot_id=target.snapshot_id,
                    target_ref=target.ref,
                    action_kind="commit",
                )
            )
            return BrowserPreparedCommit(
                target=target,
                request=request,
                context=context,
                preflight=preflight,
                payment_sources=self._current_payment_sources(page),
                protected_uses=self._current_protected_uses(page),
                attachments=self._current_attachments(page),
            )

    async def commit_prepared(
        self,
        prepared: BrowserPreparedCommit,
        transaction: BrowserTransactionEvidence,
    ) -> BrowserActionResult:
        """Revalidate and dispatch the exact semantic occurrence reviewed by the user."""

        if transaction != self.transaction_evidence(
            prepared,
            envelope_kind=transaction.envelope_kind,
            envelope_sha256=transaction.envelope_sha256,
        ):
            raise ValueError("transaction evidence does not match the prepared browser commit")
        return await self.action(
            prepared.target,
            prepared.request,
            expected_preflight=prepared.preflight,
            expected_payment_sources=prepared.payment_sources,
            expected_protected_uses=prepared.protected_uses,
            expected_attachments=prepared.attachments,
            transaction=transaction,
        )

    async def revalidate_prepared_commit(
        self,
        prepared: BrowserPreparedCommit,
        transaction: BrowserTransactionEvidence,
    ) -> None:
        """Recheck one semantic commit binding without reserving or dispatching it."""

        if transaction != self.transaction_evidence(
            prepared,
            envelope_kind=transaction.envelope_kind,
            envelope_sha256=transaction.envelope_sha256,
        ):
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="prepared browser transaction evidence changed",
                )
            )
        entry, page = await self._require_page(
            prepared.target.session_id,
            prepared.target.page_id,
        )
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            state = await page.handle.state()
            await self._page_model(entry, page, state=state)
            cached = self._cached_target(page, prepared.target)
            if cached != prepared.preflight.target:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="prepared browser semantic target changed",
                    )
                )
            await self._validate_transaction_origins(
                prepared.context.origin,
                prepared.preflight.target.frame_origin,
            )
            if self._current_payment_sources(page) != prepared.payment_sources:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="prepared browser payment-source evidence changed",
                    )
                )
            if self._current_protected_uses(page) != prepared.protected_uses:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="prepared protected-value evidence changed",
                    )
                )
            if self._current_attachments(page) != prepared.attachments:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="prepared browser attachment evidence changed",
                    )
                )
            live = await page.handle.preflight_action(
                BackendActionRequest(
                    action_id=f"browser_action_{uuid.uuid4().hex}",
                    action=prepared.request,
                    target=cached,
                )
            )
            if live != prepared.preflight:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="prepared browser target or destination changed",
                    )
                )
            await self._check_guard(
                self._guard_facts(
                    "browser_commit",
                    entry=entry,
                    page=page,
                    target_frame_origin=live.target.frame_origin,
                    effective_destinations=live.effective_destinations,
                    snapshot_id=prepared.target.snapshot_id,
                    target_ref=prepared.target.ref,
                    action_kind="commit",
                    transaction=transaction,
                )
            )

    def transaction_evidence(
        self,
        prepared: BrowserPreparedCommit | BrowserPreparedCoordinateCommit,
        *,
        envelope_kind: Literal["browser", "financial"],
        envelope_sha256: str,
    ) -> BrowserTransactionEvidence:
        """Project safe exact prepared facts into compact commit evidence."""

        top_level_origin, frame_origin = _exact_transaction_origins(
            prepared.context.origin,
            prepared.preflight.target.frame_origin,
        )
        return BrowserTransactionEvidence(
            envelope_kind=envelope_kind,
            envelope_sha256=envelope_sha256,
            top_level_origin=top_level_origin,
            target_frame_origin=frame_origin,
            effective_destinations=tuple(
                provider_safe_url(destination)
                for destination in prepared.preflight.effective_destinations
            ),
        )

    @staticmethod
    def _current_payment_sources(page: _PageEntry) -> tuple[ProfileResourceRef, ...]:
        refs = {
            item.ref.qualified: item.ref
            for item in page.protected_uses
            if item.generation == page.generation and item.kind == "payment_card"
        }
        if len(refs) != 1:
            return ()
        return tuple(refs.values())

    @staticmethod
    def _current_protected_uses(
        page: _PageEntry,
    ) -> tuple[BrowserProtectedUseBinding, ...]:
        uses = {
            (item.ref.qualified, item.revision, item.field): BrowserProtectedUseBinding(
                resource=item.ref,
                revision=item.revision,
                field=item.field,
            )
            for item in page.protected_uses
            if item.generation == page.generation
        }
        return tuple(uses[key] for key in sorted(uses))

    @staticmethod
    def _current_attachments(
        page: _PageEntry,
    ) -> tuple[BrowserAttachmentUseBinding, ...]:
        if any(
            generation == page.generation
            for generation in page.uncertain_attachment_targets.values()
        ):
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message=(
                        "browser attachment state is uncertain; navigate or definitively "
                        "replace the affected file selection before committing"
                    ),
                )
            )
        uses = {
            item.id: item
            for upload in page.uploaded_attachments.values()
            if upload.generation == page.generation
            for item in upload.attachments
        }
        return tuple(uses[key] for key in sorted(uses))

    async def protected_action_context(
        self,
        target: BrowserActionTarget,
        *,
        protected_resource: ProfileResourceRef | None = None,
        protected_field: str | None = None,
    ) -> BrowserActionContext:
        """Derive current protected-control facts locally before materialization."""
        entry, page = await self._require_page(target.session_id, target.page_id)
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            state = await page.handle.state()
            await self._page_model(entry, page, state=state)
            cached = self._cached_target(page, target)
            failure = self._validate_protected_target(cached)
            if failure is not None:
                raise BrowserError(failure)
            if cached.frame_origin is None:
                raise BrowserError(
                    BrowserFailure(
                        code="incompatible_target",
                        message="protected browser target has no exact frame origin",
                    )
                )
            await self._validate_destination(cached.frame_origin)
            live = await page.handle.preflight_protected_target(cached)
            if live != cached:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="protected browser target changed; request a new snapshot",
                    )
                )
            if self._runtime_guard is not None and (
                protected_resource is None or protected_field is None
            ):
                raise BrowserError(
                    BrowserFailure(
                        code="unattended_denied",
                        message="background protected fill requires an exact resource and field",
                    )
                )
            if protected_resource is not None and protected_field is not None:
                guard_facts = self._guard_facts(
                    "browser_fill_protected",
                    entry=entry,
                    page=page,
                    target_frame_origin=cached.frame_origin,
                    snapshot_id=target.snapshot_id,
                    target_ref=target.ref,
                    action_kind="protected_fill",
                    protected_resource=protected_resource,
                    protected_field=protected_field,
                )
                await self._reserve_guard("protected_materializations", 1, guard_facts)
            return self.action_context(target)

    async def protected_fill(
        self,
        target: BrowserActionTarget,
        material: ProtectedMaterial,
        broker: ProtectedValueBroker,
    ) -> BrowserActionResult:
        """Revalidate and dispatch one exact in-process protected value once."""
        request = material.use.request
        if request.occurrence != self.protected_occurrence(target):
            raise ValueError("prepared protected value does not belong to this browser target")
        entry, page = await self._require_page(target.session_id, target.page_id)
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            state_before = await page.handle.state()
            await self._page_model(entry, page, state=state_before)
            action_id = f"browser_action_{uuid.uuid4().hex}"
            guard_facts = self._guard_facts(
                "browser_fill_protected",
                entry=entry,
                page=page,
                target_frame_origin=request.frame_origin,
                snapshot_id=target.snapshot_id,
                target_ref=target.ref,
                action_kind="protected_fill",
                protected_resource=material.descriptor.ref,
                protected_revision=material.descriptor.revision,
                protected_field=material.field.name,
            )
            await self._check_guard(guard_facts)
            try:
                cached = self._cached_target(page, target)
            except BrowserError as exc:
                return await self._protected_rejection(
                    entry, page, action_id, material, exc.failure, state_before
                )
            if failure := self._validate_protected_target(cached):
                return await self._protected_rejection(
                    entry, page, action_id, material, failure, state_before
                )
            origin: str | None = None
            with suppress(ValueError):
                origin = canonical_origin(state_before.url)
            if origin != request.top_level_origin or cached.frame_origin != request.frame_origin:
                return await self._protected_rejection(
                    entry,
                    page,
                    action_id,
                    material,
                    BrowserFailure(
                        code="stale_target",
                        message="protected browser destination changed after review",
                    ),
                    state_before,
                )
            assert cached.frame_origin is not None
            try:
                await self._validate_destination(cached.frame_origin)
                live = await page.handle.preflight_protected_target(cached)
                if live != cached:
                    raise BrowserError(
                        BrowserFailure(
                            code="stale_target",
                            message="protected browser target changed after review",
                        )
                    )
                await broker.revalidate(material)
            except BrowserError as exc:
                return await self._protected_rejection(
                    entry, page, action_id, material, exc.failure, state_before
                )
            except Exception:
                return await self._protected_rejection(
                    entry,
                    page,
                    action_id,
                    material,
                    BrowserFailure(
                        code="protected_field",
                        message="protected-value authorization changed after review",
                    ),
                    state_before,
                )
            backend_request = BackendProtectedFillRequest(
                action_id=action_id,
                target=cached,
                value=material.value,
            )
            await self._reserve_guard("navigations", 1, guard_facts)
            await self._reserve_possible_pages(entry, guard_facts)
            before_page_ids = frozenset(item.id for item in entry.pages_by_key.values())
            try:
                outcome = await page.handle.perform_protected_fill(backend_request)
            except asyncio.CancelledError:
                self._record_interrupted_effect(
                    page,
                    action_id,
                    "protected_fill",
                    protected_ref=material.descriptor.ref,
                    protected_field=material.field.name,
                )
                latest = page.latest_action
                assert latest is not None and latest.failure is not None
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=latest.failure,
                )
                raise
            except Exception:
                outcome = BackendActionOutcome(
                    disposition="in_doubt",
                    dispatch_state="dispatched",
                    state_before=state_before,
                    failure=BrowserFailure(
                        code="action_in_doubt",
                        message=(
                            "protected browser fill failed after dispatch may have begun; "
                            "inspect the page before continuing"
                        ),
                        outcome_uncertain=True,
                    ),
                )
            generation_before = page.generation
            if outcome.dispatch_state != "not_dispatched" and not outcome.navigation_occurred:
                page.protected_uses[:] = [
                    item
                    for item in page.protected_uses
                    if not (
                        item.generation == generation_before
                        and item.frame_key == cached.frame_key
                        and item.target_ref == cached.ref
                    )
                ]
                if outcome.disposition == "performed":
                    page.protected_uses.append(
                        _ProtectedPageUse(
                            ref=material.descriptor.ref,
                            kind=material.descriptor.kind,
                            generation=generation_before,
                            frame_key=cached.frame_key,
                            target_ref=cached.ref,
                            field=material.field.name,
                            revision=material.descriptor.revision,
                        )
                    )
                    page.protected_uses[:] = page.protected_uses[-50:]
            result = await self._finalize_special_action(
                entry,
                page,
                action_id=action_id,
                kind="protected_fill",
                outcome=outcome,
                protected_ref=material.descriptor.ref,
                protected_field=material.field.name,
                before_page_ids=before_page_ids,
            )
            await self._record_guard(
                guard_facts,
                disposition=result.disposition,
                action_id=action_id,
                failure=result.failure,
                created_page_count=len(result.postcondition.page_changes.created_page_ids),
            )
            return result

    @staticmethod
    def protected_occurrence(target: BrowserActionTarget) -> str:
        """Return the safe exact occurrence bound into broker and effect evidence."""
        return f"{target.session_id}/{target.page_id}/{target.snapshot_id}/{target.ref}"

    def coordinate_context(self, target: BrowserCoordinateTarget) -> BrowserCoordinateContext:
        """Return exact cached visual facts for destructive permission review."""
        entry = self._require_session(target.session_id)
        page = self._page_entry(entry, target.page_id)
        visual = page.visual
        if visual is None or visual.snapshot_id != target.screenshot_id:
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="browser visual snapshot is stale; request a new one",
                )
            )
        if not (0 <= target.x < visual.composed.width and 0 <= target.y < visual.composed.height):
            raise BrowserError(
                BrowserFailure(
                    code="coordinate_out_of_bounds",
                    message="browser coordinate is outside the disclosed image",
                )
            )
        origin = None
        if page.exact_url != "about:blank":
            with suppress(ValueError):
                origin = canonical_origin(page.exact_url)
        return BrowserCoordinateContext(
            target=target,
            resource=entry.resource,
            navigation_generation=page.generation,
            url=provider_safe_url(page.exact_url),
            origin=origin,
            image_width=visual.composed.width,
            image_height=visual.composed.height,
            css_x=target.x * visual.viewport.width / visual.composed.width,
            css_y=target.y * visual.viewport.height / visual.composed.height,
            masked_base_sha256=visual.masked_base_sha256,
        )

    async def prepare_coordinate_commit(
        self,
        target: BrowserCoordinateTarget,
        *,
        dialog: BrowserDialogPolicy,
    ) -> BrowserPreparedCoordinateCommit:
        """Freeze one live visual commit occurrence without dispatching it."""

        prepared = await self._prepare_coordinate(
            target,
            dialog=dialog,
            tool_name="browser_coordinate_commit",
            transaction=True,
        )
        assert isinstance(prepared, BrowserPreparedCoordinateCommit)
        return prepared

    async def prepare_coordinate_click(
        self,
        target: BrowserCoordinateTarget,
    ) -> BrowserPreparedCoordinateClick:
        """Freeze one harness-verified semantic-first ordinary coordinate click."""

        prepared = await self._prepare_coordinate(
            target,
            dialog=BrowserDialogPolicy(),
            tool_name="browser_coordinate_click",
            transaction=False,
        )
        assert isinstance(prepared, BrowserPreparedCoordinateClick)
        return prepared

    async def _prepare_coordinate(
        self,
        target: BrowserCoordinateTarget,
        *,
        dialog: BrowserDialogPolicy,
        tool_name: Literal["browser_coordinate_click", "browser_coordinate_commit"],
        transaction: bool,
    ) -> BrowserPreparedCoordinateClick | BrowserPreparedCoordinateCommit:
        """Resolve an exact coordinate only after harness-owned semantic preflight."""

        entry, page = await self._require_page(target.session_id, target.page_id)
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            state = await page.handle.state()
            await self._page_model(entry, page, state=state)
            semantic = page.semantic
            semantic_required = not transaction or self._runtime_guard is not None
            if semantic_required and (
                semantic is None or semantic.navigation_generation != page.generation
            ):
                raise BrowserError(
                    BrowserFailure(
                        code="semantic_snapshot_required",
                        message=(
                            "coordinate fallback requires a current semantic snapshot before "
                            "visual fallback"
                        ),
                    )
                )
            context = self.coordinate_context(target)
            visual = page.visual
            assert visual is not None
            if semantic_required:
                if visual.admitted_provider is None:
                    raise BrowserError(
                        BrowserFailure(
                            code="screenshot_denied",
                            message="coordinate fallback has no provider disclosure",
                        )
                    )
                self._require_screenshot_disclosure(entry, visual.admitted_provider)
            request = BackendCoordinateRequest(
                action_id=f"browser_action_{uuid.uuid4().hex}",
                x=context.css_x,
                y=context.css_y,
                masked_base_sha256=visual.masked_base_sha256,
                viewport=visual.viewport,
                dialog_response=dialog.response,
                dialog_prompt_text=dialog.prompt_text,
            )
            try:
                preflight = await page.handle.preflight_coordinate_commit(request)
            except BrowserError:
                raise
            except Exception as exc:
                raise BrowserError(
                    BrowserFailure(
                        code="backend_error",
                        message="browser coordinate preflight failed before dispatch",
                    )
                ) from exc
            if transaction:
                await self._validate_transaction_origins(
                    context.origin,
                    preflight.target.frame_origin,
                )
            else:
                if context.origin is not None:
                    await self._validate_destination(context.origin)
                if preflight.target.frame_origin is not None:
                    await self._validate_destination(preflight.target.frame_origin)
                if preflight.target.consequential or preflight.financial_signal:
                    raise BrowserError(
                        BrowserFailure(
                            code="consequential_target",
                            message=(
                                "consequential coordinate targets require a transaction envelope"
                            ),
                        )
                    )
            fallback: CoordinateFallbackEvidence | None = None
            if semantic_required:
                assert semantic is not None
                equivalent = preflight.equivalent_semantic_ref
                failed = page.semantic_preflight_failure
                if (
                    equivalent is not None
                    and failed is not None
                    and failed.target.ref == equivalent
                    and failed.action_kind == ("commit" if transaction else "click")
                ):
                    fallback = CoordinateFallbackEvidence(
                        semantic_snapshot_id=semantic.snapshot_id,
                        reason="semantic_preflight_not_dispatched",
                        failed_semantic_target=failed.target,
                        semantic_failure_code=failed.failure_code,
                    )
                elif equivalent is not None:
                    refreshed = await self._snapshot_locked(entry, page)
                    refreshed_semantic = page.semantic
                    assert refreshed_semantic is not None
                    candidates = refreshed_semantic.targets_by_ref.get(equivalent, ())
                    if len(candidates) != 1:
                        raise BrowserError(
                            BrowserFailure(
                                code="semantic_snapshot_required",
                                message=(
                                    "equivalent semantic target evidence is no longer current"
                                ),
                            )
                        )
                    replacement = BrowserActionTarget(
                        session_id=entry.id,
                        page_id=page.id,
                        snapshot_id=refreshed.snapshot_id,
                        ref=equivalent,
                    )
                    raise BrowserError(
                        BrowserFailure(
                            code="semantic_target_available",
                            message=(
                                "an equivalent semantic target is available; use its semantic tool"
                            ),
                            replacement_target=replacement,
                        )
                    )
                else:
                    fallback = CoordinateFallbackEvidence(
                        semantic_snapshot_id=semantic.snapshot_id,
                        reason=(
                            "custom_rendered_target"
                            if preflight.target.role == "canvas"
                            else "no_supported_semantic_target"
                        ),
                    )
                await self._check_guard(
                    self._guard_facts(
                        tool_name,
                        entry=entry,
                        page=page,
                        target_frame_origin=preflight.target.frame_origin,
                        effective_destinations=preflight.effective_destinations,
                        provider=visual.admitted_provider,
                        snapshot_id=target.screenshot_id,
                        target_ref=preflight.target.ref,
                        action_kind=("coordinate_commit" if transaction else "coordinate_click"),
                        coordinate_fallback=fallback,
                    )
                )
            assert fallback is not None or not semantic_required
            if transaction:
                return BrowserPreparedCoordinateCommit(
                    target=target,
                    dialog=dialog,
                    context=context,
                    viewport=visual.viewport,
                    preflight=preflight,
                    payment_sources=self._current_payment_sources(page),
                    protected_uses=self._current_protected_uses(page),
                    attachments=self._current_attachments(page),
                    fallback=fallback,
                )
            if fallback is None:
                raise BrowserError(
                    BrowserFailure(
                        code="semantic_snapshot_required",
                        message=(
                            "ordinary coordinate clicks require harness-issued fallback evidence"
                        ),
                    )
                )
            return BrowserPreparedCoordinateClick(
                target=target,
                context=context,
                viewport=visual.viewport,
                preflight=preflight,
                fallback=fallback,
            )

    async def _validate_transaction_origins(
        self,
        top_level_origin: str | None,
        target_frame_origin: str | None,
    ) -> None:
        """Require exact, currently allowed origins for a reviewed commit."""

        top_level_origin, target_frame_origin = _exact_transaction_origins(
            top_level_origin,
            target_frame_origin,
        )
        await self._validate_destination(top_level_origin)
        await self._validate_destination(target_frame_origin)

    async def coordinate_commit_prepared(
        self,
        prepared: BrowserPreparedCoordinateCommit,
        transaction: BrowserTransactionEvidence,
    ) -> BrowserActionResult:
        """Revalidate and dispatch the exact visual occurrence reviewed by the user."""

        if transaction != self.transaction_evidence(
            prepared,
            envelope_kind=transaction.envelope_kind,
            envelope_sha256=transaction.envelope_sha256,
        ):
            raise ValueError("transaction evidence does not match the prepared coordinate commit")
        return await self.coordinate_commit(
            prepared.target,
            dialog=prepared.dialog,
            expected_preflight=prepared.preflight,
            expected_payment_sources=prepared.payment_sources,
            expected_protected_uses=prepared.protected_uses,
            expected_attachments=prepared.attachments,
            transaction=transaction,
            expected_fallback=prepared.fallback,
        )

    async def revalidate_prepared_coordinate_commit(
        self,
        prepared: BrowserPreparedCoordinateCommit,
        transaction: BrowserTransactionEvidence,
    ) -> None:
        """Recheck one coordinate commit binding without reserving or dispatching it."""

        if prepared.fallback is None and self._runtime_guard is not None:
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="prepared coordinate fallback evidence is missing",
                )
            )
        if transaction != self.transaction_evidence(
            prepared,
            envelope_kind=transaction.envelope_kind,
            envelope_sha256=transaction.envelope_sha256,
        ):
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="prepared coordinate transaction evidence changed",
                )
            )
        entry, page = await self._require_page(
            prepared.target.session_id,
            prepared.target.page_id,
        )
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            state = await page.handle.state()
            await self._page_model(entry, page, state=state)
            context = self.coordinate_context(prepared.target)
            visual = page.visual
            assert visual is not None
            if self._runtime_guard is not None:
                if visual.admitted_provider is None:
                    raise BrowserError(
                        BrowserFailure(
                            code="screenshot_denied",
                            message="coordinate fallback has no provider disclosure",
                        )
                    )
                self._require_screenshot_disclosure(entry, visual.admitted_provider)
            if self._current_payment_sources(page) != prepared.payment_sources:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="prepared browser payment-source evidence changed",
                    )
                )
            if self._current_protected_uses(page) != prepared.protected_uses:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="prepared protected-value evidence changed",
                    )
                )
            if self._current_attachments(page) != prepared.attachments:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="prepared browser attachment evidence changed",
                    )
                )
            live = await page.handle.preflight_coordinate_commit(
                BackendCoordinateRequest(
                    action_id=f"browser_action_{uuid.uuid4().hex}",
                    x=context.css_x,
                    y=context.css_y,
                    masked_base_sha256=visual.masked_base_sha256,
                    viewport=visual.viewport,
                    dialog_response=prepared.dialog.response,
                    dialog_prompt_text=prepared.dialog.prompt_text,
                )
            )
            if live != prepared.preflight:
                raise BrowserError(
                    BrowserFailure(
                        code="stale_target",
                        message="prepared coordinate target or destination changed",
                    )
                )
            await self._validate_transaction_origins(
                context.origin,
                live.target.frame_origin,
            )
            await self._check_guard(
                self._guard_facts(
                    "browser_coordinate_commit",
                    entry=entry,
                    page=page,
                    target_frame_origin=live.target.frame_origin,
                    effective_destinations=live.effective_destinations,
                    provider=visual.admitted_provider,
                    snapshot_id=prepared.target.screenshot_id,
                    target_ref=live.target.ref,
                    action_kind="coordinate_commit",
                    transaction=transaction,
                    coordinate_fallback=prepared.fallback,
                )
            )

    async def coordinate_click_prepared(
        self,
        prepared: BrowserPreparedCoordinateClick,
    ) -> BrowserActionResult:
        """Dispatch one exact harness-issued ordinary coordinate fallback."""

        return await self._coordinate_action(
            prepared.target,
            action_kind="coordinate_click",
            dialog=BrowserDialogPolicy(),
            expected_preflight=prepared.preflight,
            expected_payment_sources=None,
            expected_protected_uses=None,
            expected_attachments=None,
            transaction=None,
            expected_fallback=prepared.fallback,
        )

    async def upload(
        self,
        target: BrowserActionTarget,
        files: tuple[LoadedAttachment, ...],
        *,
        attachment_ids: tuple[str, ...] = (),
    ) -> BrowserActionResult:
        """Dispatch exact prepared bytes to one current file control."""
        entry, page = await self._require_page(target.session_id, target.page_id)
        request = BrowserActionRequest(kind="upload")
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            if self._runtime_guard is not None and len(attachment_ids) != len(files):
                raise BrowserError(
                    BrowserFailure(
                        code="unattended_denied",
                        message="background uploads require exact approved attachment ids",
                    )
                )
            state_before = await page.handle.state()
            await self._page_model(entry, page, state=state_before)
            action_id = f"browser_action_{uuid.uuid4().hex}"
            await self._check_guard(
                self._guard_facts(
                    "browser_upload",
                    entry=entry,
                    page=page,
                    snapshot_id=target.snapshot_id,
                    target_ref=target.ref,
                    action_kind="upload",
                    attachment_count=len(files),
                    attachment_ids=attachment_ids,
                    attachment_sha256=tuple(item.sha256 for item in files),
                    byte_count=sum(item.size_bytes for item in files),
                )
            )
            try:
                cached = self._cached_target(page, target)
            except BrowserError as exc:
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=exc.failure,
                    state=state_before,
                )
            if not cached.file or cached.disabled:
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="incompatible_target",
                        message="browser upload requires a current enabled file control",
                    ),
                    state=state_before,
                )
            if len(files) > 1 and not cached.multiple:
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="incompatible_target",
                        message="browser file control does not accept multiple files",
                    ),
                    state=state_before,
                )
            backend_request = BackendActionRequest(
                action_id=action_id,
                action=request,
                target=cached,
            )
            guard_facts = self._guard_facts(
                "browser_upload",
                entry=entry,
                page=page,
                target_frame_origin=cached.frame_origin,
                snapshot_id=target.snapshot_id,
                target_ref=target.ref,
                action_kind="upload",
                attachment_count=len(files),
                attachment_ids=attachment_ids,
                attachment_sha256=tuple(item.sha256 for item in files),
                byte_count=sum(item.size_bytes for item in files),
            )
            await self._reserve_guard("uploads", 1, guard_facts)
            await self._reserve_guard(
                "upload_bytes",
                guard_facts.byte_count,
                guard_facts,
            )
            await self._reserve_guard("navigations", 1, guard_facts)
            await self._reserve_possible_pages(entry, guard_facts)
            before_page_ids = frozenset(item.id for item in entry.pages_by_key.values())
            try:
                outcome = await page.handle.perform_upload(
                    backend_request,
                    tuple(
                        BackendUploadFile(
                            filename=item.filename,
                            media_type=item.media_type,
                            content=item.content,
                            sha256=item.sha256,
                        )
                        for item in files
                    ),
                )
            except asyncio.CancelledError:
                page.uncertain_attachment_targets[target.ref] = page.generation
                self._record_interrupted_effect(page, action_id, "upload")
                latest = page.latest_action
                assert latest is not None and latest.failure is not None
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=latest.failure,
                )
                raise
            result = await self._finalize_special_action(
                entry,
                page,
                action_id=action_id,
                kind="upload",
                outcome=outcome,
                before_page_ids=before_page_ids,
            )
            if result.disposition == "performed" and attachment_ids:
                page.uploaded_attachments[target.ref] = _UploadedPageUse(
                    generation=page.generation,
                    target_ref=target.ref,
                    attachments=tuple(
                        BrowserAttachmentUseBinding(
                            id=attachment_id,
                            sha256=file.sha256,
                            byte_count=file.size_bytes,
                        )
                        for attachment_id, file in zip(
                            attachment_ids,
                            files,
                            strict=True,
                        )
                    ),
                )
                page.uncertain_attachment_targets.pop(target.ref, None)
            elif result.disposition == "performed":
                page.uncertain_attachment_targets.pop(target.ref, None)
            elif result.disposition == "in_doubt":
                page.uncertain_attachment_targets[target.ref] = page.generation
            await self._record_guard(
                guard_facts,
                disposition=result.disposition,
                action_id=action_id,
                failure=result.failure,
                created_page_count=len(result.postcondition.page_changes.created_page_ids),
            )
            return result

    async def download(
        self,
        target: BrowserActionTarget,
    ) -> BrowserDownloadResult:
        """Retain one explicit owned-session download under its source profile."""
        entry, page = await self._require_page(target.session_id, target.page_id)
        request = BrowserActionRequest(kind="download")
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            state_before = await page.handle.state()
            await self._page_model(entry, page, state=state_before)
            action_id = f"browser_action_{uuid.uuid4().hex}"
            await self._check_guard(
                self._guard_facts(
                    "browser_download",
                    entry=entry,
                    page=page,
                    snapshot_id=target.snapshot_id,
                    target_ref=target.ref,
                    action_kind="download",
                )
            )
            if entry.mode == "attached_cdp" or entry.download_temp_dir is None:
                return BrowserDownloadResult(
                    action_id=action_id,
                    disposition="not_performed",
                    page=self._page_from_state(entry, page, state_before),
                    failure=BrowserFailure(
                        code="download_unavailable",
                        message="downloads are unavailable for attached browser sessions",
                    ),
                )
            try:
                cached = self._cached_target(page, target)
            except BrowserError as exc:
                return BrowserDownloadResult(
                    action_id=action_id,
                    disposition="not_performed",
                    page=self._page_from_state(entry, page, state_before),
                    failure=exc.failure,
                )
            backend_request = BackendActionRequest(
                action_id=action_id,
                action=request,
                target=cached,
            )
            guard_facts = self._guard_facts(
                "browser_download",
                entry=entry,
                page=page,
                target_frame_origin=cached.frame_origin,
                snapshot_id=target.snapshot_id,
                target_ref=target.ref,
                action_kind="download",
            )
            await self._reserve_guard("downloads", 1, guard_facts)
            await self._reserve_guard("navigations", 1, guard_facts)
            await self._reserve_possible_pages(entry, guard_facts)
            before_page_ids = frozenset(item.id for item in entry.pages_by_key.values())
            try:
                backend = await page.handle.perform_download(backend_request)
            except asyncio.CancelledError:
                self._record_interrupted_effect(page, action_id, "download")
                latest = page.latest_action
                assert latest is not None and latest.failure is not None
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=latest.failure,
                )
                raise
            outcome = backend.action
            if outcome.dispatch_state != "not_dispatched":
                self._invalidate_snapshot(page)
            if outcome.navigation_occurred:
                page.generation += 1
                page.protected_uses.clear()
            created_page_count = 0
            try:
                page_sync = await self._sync_pages(entry, discard_blocked_owned=True)
                after_page_ids = {item.id for item in entry.pages_by_key.values()}
                created_page_count = (
                    len(after_page_ids - before_page_ids)
                    + page_sync.discarded_page_count
                    + page_sync.blocked_page_count
                )
            except asyncio.CancelledError:
                self._record_interrupted_effect(page, action_id, "download")
                latest = page.latest_action
                assert latest is not None and latest.failure is not None
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=latest.failure,
                )
                raise
            except Exception:
                outcome = BackendActionOutcome(
                    disposition="in_doubt",
                    dispatch_state="dispatched",
                    state_before=outcome.state_before,
                    state_after=outcome.state_after,
                    navigation_occurred=outcome.navigation_occurred,
                    dialogs=outcome.dialogs,
                    failure=BrowserFailure(
                        code="action_in_doubt",
                        message=(
                            "browser download completed but page changes could not be observed"
                        ),
                        outcome_uncertain=True,
                    ),
                )
            reference: BrowserDownloadRef | None = None
            failure = outcome.failure
            disposition = outcome.disposition
            if disposition == "performed" and backend.download is not None:
                interrupted = False
                publication_error: Exception | None
                try:
                    download_bytes = backend.download.temporary_path.lstat().st_size
                    publication_facts = guard_facts.model_copy(
                        update={"byte_count": download_bytes}
                    )
                    await self._reserve_guard(
                        "download_bytes",
                        download_bytes,
                        publication_facts,
                    )
                    publication = asyncio.create_task(
                        asyncio.to_thread(
                            self._publish_download,
                            entry,
                            backend.download.temporary_path,
                            backend.download.suggested_filename,
                            backend.download.media_type,
                        )
                    )
                    reference, publication_error, interrupted = await _join_download_publication(
                        publication
                    )
                    if publication_error is not None:
                        raise publication_error
                except _DownloadPublicationTooLarge:
                    disposition = "in_doubt"
                    failure = BrowserFailure(
                        code="download_too_large",
                        message="browser download exceeded the configured publication limit",
                        outcome_uncertain=True,
                    )
                except BrowserError as exc:
                    disposition = "in_doubt"
                    failure = exc.failure.model_copy(update={"outcome_uncertain": True})
                except Exception:
                    disposition = "in_doubt"
                    failure = BrowserFailure(
                        code="download_publish_failed",
                        message="browser download could not be safely published",
                        outcome_uncertain=True,
                    )
                finally:
                    backend.download.temporary_path.unlink(missing_ok=True)
                if interrupted:
                    self._record_interrupted_effect(page, action_id, "download")
                    latest = page.latest_action
                    assert latest is not None and latest.failure is not None
                    await self._record_guard(
                        guard_facts,
                        disposition="in_doubt",
                        action_id=action_id,
                        failure=latest.failure,
                    )
                    raise asyncio.CancelledError
            elif disposition == "performed":
                disposition = "in_doubt"
                failure = BrowserFailure(
                    code="download_unavailable",
                    message="browser action completed without retained download evidence",
                    outcome_uncertain=True,
                )
            elif backend.download is not None:
                backend.download.temporary_path.unlink(missing_ok=True)
            evidence = BrowserActionEvidence(
                action_id=action_id,
                kind="download",
                disposition=disposition,
                failure=failure,
            )
            page.latest_action = evidence
            state = outcome.state_after or outcome.state_before
            model = self._page_from_state(entry, page, state)
            result = BrowserDownloadResult(
                action_id=action_id,
                disposition=disposition,
                page=model,
                download=reference,
                failure=failure,
            )
            await self._record_guard(
                guard_facts,
                disposition=disposition,
                action_id=action_id,
                failure=failure,
                result_byte_count=reference.size_bytes if reference is not None else 0,
                created_page_count=created_page_count,
            )
            return result

    async def coordinate_commit(
        self,
        target: BrowserCoordinateTarget,
        *,
        dialog: BrowserDialogPolicy,
        expected_preflight: BackendCoordinatePreflight | None = None,
        expected_payment_sources: tuple[ProfileResourceRef, ...] | None = None,
        expected_protected_uses: tuple[BrowserProtectedUseBinding, ...] | None = None,
        expected_attachments: tuple[BrowserAttachmentUseBinding, ...] | None = None,
        transaction: BrowserTransactionEvidence | None = None,
        expected_fallback: CoordinateFallbackEvidence | None = None,
    ) -> BrowserActionResult:
        """Click one exact point only after same-generation pixel preflight."""

        if transaction is None or expected_preflight is None:
            raise ValueError(
                "coordinate commits require prepared preflight and transaction evidence"
            )
        if self._runtime_guard is not None and expected_fallback is None:
            raise ValueError("background coordinate commits require fallback evidence")
        return await self._coordinate_action(
            target,
            action_kind="coordinate_commit",
            dialog=dialog,
            expected_preflight=expected_preflight,
            expected_payment_sources=expected_payment_sources,
            expected_protected_uses=expected_protected_uses,
            expected_attachments=expected_attachments,
            transaction=transaction,
            expected_fallback=expected_fallback,
        )

    async def _coordinate_action(
        self,
        target: BrowserCoordinateTarget,
        *,
        action_kind: Literal["coordinate_click", "coordinate_commit"],
        dialog: BrowserDialogPolicy,
        expected_preflight: BackendCoordinatePreflight,
        expected_payment_sources: tuple[ProfileResourceRef, ...] | None,
        expected_protected_uses: tuple[BrowserProtectedUseBinding, ...] | None,
        expected_attachments: tuple[BrowserAttachmentUseBinding, ...] | None,
        transaction: BrowserTransactionEvidence | None,
        expected_fallback: CoordinateFallbackEvidence | None,
    ) -> BrowserActionResult:
        """Revalidate and dispatch one exact ordinary or consequential coordinate click."""

        if (action_kind == "coordinate_commit") != (transaction is not None):
            raise ValueError("only coordinate commits carry transaction evidence")
        entry, page = await self._require_page(target.session_id, target.page_id)
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            state_before = await page.handle.state()
            await self._page_model(entry, page, state=state_before)
            action_id = f"browser_action_{uuid.uuid4().hex}"
            request = BrowserActionRequest(kind=action_kind, dialog=dialog)
            try:
                context = self.coordinate_context(target)
            except BrowserError as exc:
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=exc.failure,
                    state=state_before,
                    transaction=transaction,
                )
            visual = page.visual
            assert visual is not None
            if self._runtime_guard is not None or action_kind == "coordinate_click":
                if visual.admitted_provider is None:
                    raise BrowserError(
                        BrowserFailure(
                            code="screenshot_denied",
                            message="coordinate fallback has no provider disclosure",
                        )
                    )
                self._require_screenshot_disclosure(entry, visual.admitted_provider)
            if (
                expected_payment_sources is not None
                and self._current_payment_sources(page) != expected_payment_sources
            ):
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="stale_target",
                        message=(
                            "browser payment-source evidence changed after transaction review; "
                            "request a new visual snapshot"
                        ),
                    ),
                    state=state_before,
                    transaction=transaction,
                )
            if (
                expected_protected_uses is not None
                and self._current_protected_uses(page) != expected_protected_uses
            ):
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="stale_target",
                        message=(
                            "browser protected-value evidence changed after transaction review"
                        ),
                    ),
                    state=state_before,
                    transaction=transaction,
                )
            if (
                expected_attachments is not None
                and self._current_attachments(page) != expected_attachments
            ):
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="stale_target",
                        message="browser attachment evidence changed after transaction review",
                    ),
                    state=state_before,
                    transaction=transaction,
                )
            guard_facts = self._guard_facts(
                (
                    "browser_coordinate_commit"
                    if action_kind == "coordinate_commit"
                    else "browser_coordinate_click"
                ),
                entry=entry,
                page=page,
                target_frame_origin=expected_preflight.target.frame_origin,
                effective_destinations=expected_preflight.effective_destinations,
                provider=visual.admitted_provider,
                snapshot_id=target.screenshot_id,
                target_ref=expected_preflight.target.ref,
                action_kind=action_kind,
                transaction=transaction,
                coordinate_fallback=expected_fallback,
            )
            await self._reserve_guard(
                "transaction_commits" if action_kind == "coordinate_commit" else "interactions",
                1,
                guard_facts,
            )
            await self._reserve_guard("navigations", 1, guard_facts)
            await self._reserve_possible_pages(entry, guard_facts)
            before_page_ids = frozenset(item.id for item in entry.pages_by_key.values())
            try:
                outcome = await page.handle.perform_coordinate_commit(
                    BackendCoordinateRequest(
                        action_id=action_id,
                        x=context.css_x,
                        y=context.css_y,
                        masked_base_sha256=visual.masked_base_sha256,
                        viewport=visual.viewport,
                        dialog_response=dialog.response,
                        dialog_prompt_text=dialog.prompt_text,
                    ),
                    expected=expected_preflight,
                )
            except asyncio.CancelledError:
                self._record_interrupted_effect(
                    page,
                    action_id,
                    action_kind,
                    transaction=transaction,
                )
                latest = page.latest_action
                assert latest is not None and latest.failure is not None
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=latest.failure,
                )
                raise
            except Exception:
                self._invalidate_snapshot(page)
                failure = BrowserFailure(
                    code="action_in_doubt",
                    message=(
                        "browser coordinate action failed after dispatch may have begun; "
                        "inspect the page before continuing"
                    ),
                    outcome_uncertain=True,
                )
                page.latest_action = BrowserActionEvidence(
                    action_id=action_id,
                    kind=action_kind,
                    disposition="in_doubt",
                    failure=failure,
                    transaction=transaction,
                )
                page.protected_uses.clear()
                result = BrowserActionResult(
                    action_id=action_id,
                    kind=action_kind,
                    disposition="in_doubt",
                    page=self._page_from_state(entry, page, state_before),
                    failure=failure,
                    postcondition=BrowserPostcondition(observation_limited=True),
                    transaction=transaction,
                )
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=failure,
                )
                return result
            if outcome.failure is not None and outcome.failure.code == "stale_target":
                self._invalidate_snapshot(page)
            result = await self._finalize_special_action(
                entry,
                page,
                action_id=action_id,
                kind=action_kind,
                outcome=outcome,
                transaction=transaction,
                before_page_ids=before_page_ids,
            )
            await self._record_guard(
                guard_facts,
                disposition=result.disposition,
                action_id=action_id,
                failure=result.failure,
                created_page_count=len(result.postcondition.page_changes.created_page_ids),
            )
            return result

    async def action(
        self,
        target: BrowserActionTarget,
        request: BrowserActionRequest,
        *,
        expected_preflight: BackendActionPreflight | None = None,
        expected_payment_sources: tuple[ProfileResourceRef, ...] | None = None,
        expected_protected_uses: tuple[BrowserProtectedUseBinding, ...] | None = None,
        expected_attachments: tuple[BrowserAttachmentUseBinding, ...] | None = None,
        transaction: BrowserTransactionEvidence | None = None,
    ) -> BrowserActionResult:
        """Validate and dispatch one browser action without automatic replay."""

        if request.kind == "commit":
            if transaction is None or expected_preflight is None:
                raise ValueError(
                    "semantic commits require prepared preflight and transaction evidence"
                )
        elif (
            transaction is not None
            or expected_payment_sources is not None
            or expected_protected_uses is not None
            or expected_attachments is not None
        ):
            raise ValueError("transaction evidence requires a semantic commit action")

        entry, page = await self._require_page(target.session_id, target.page_id)
        async with entry.action_lock, page.lock:
            self._ensure_session_active(entry)
            state_before = await page.handle.state()
            if state_before.closed:
                raise BrowserError(
                    BrowserFailure(code="page_closed", message="browser page is closed")
                )
            await self._page_model(entry, page, state=state_before)
            action_id = f"browser_action_{uuid.uuid4().hex}"
            initial_guard_facts = self._guard_facts(
                _ACTION_TO_TOOL[request.kind],
                entry=entry,
                page=page,
                snapshot_id=target.snapshot_id,
                target_ref=target.ref,
                action_kind=request.kind,
                transaction=transaction,
            )
            await self._check_guard(initial_guard_facts)
            try:
                cached = self._cached_target(page, target)
            except BrowserError as exc:
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=exc.failure,
                    state=state_before,
                    transaction=transaction,
                )

            if failure := self._validate_action(cached, request):
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=failure,
                    state=state_before,
                    transaction=transaction,
                )
            if cached.frame_origin is not None:
                try:
                    await self._validate_destination(cached.frame_origin)
                except BrowserError as exc:
                    return await self._rejected_action(
                        entry,
                        page,
                        action_id=action_id,
                        request=request,
                        failure=exc.failure,
                        state=state_before,
                        transaction=transaction,
                    )

            backend_request = BackendActionRequest(
                action_id=action_id,
                action=request,
                target=cached,
            )
            try:
                preflight = await page.handle.preflight_action(backend_request)
            except BrowserError as exc:
                if request.kind == "click":
                    page.semantic_preflight_failure = _SemanticPreflightFailure(
                        target=target,
                        failure_code=exc.failure.code,
                        action_kind="click",
                    )
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=exc.failure,
                    state=state_before,
                    transaction=transaction,
                )
            except Exception:
                if request.kind == "click":
                    page.semantic_preflight_failure = _SemanticPreflightFailure(
                        target=target,
                        failure_code="backend_error",
                        action_kind="click",
                    )
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="backend_error",
                        message="browser target preflight failed before dispatch",
                    ),
                    state=state_before,
                    transaction=transaction,
                )
            if preflight.target != cached:
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="stale_target",
                        message="browser target changed; request a new snapshot",
                    ),
                    state=state_before,
                    transaction=transaction,
                )
            if expected_preflight is not None and preflight != expected_preflight:
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="stale_target",
                        message=(
                            "browser target or destination changed after transaction review; "
                            "request a new snapshot"
                        ),
                    ),
                    state=state_before,
                    transaction=transaction,
                )
            if (
                expected_payment_sources is not None
                and self._current_payment_sources(page) != expected_payment_sources
            ):
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="stale_target",
                        message=(
                            "browser payment-source evidence changed after transaction review; "
                            "request a new snapshot"
                        ),
                    ),
                    state=state_before,
                    transaction=transaction,
                )
            if (
                expected_protected_uses is not None
                and self._current_protected_uses(page) != expected_protected_uses
            ):
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="stale_target",
                        message=(
                            "browser protected-value evidence changed after transaction review"
                        ),
                    ),
                    state=state_before,
                    transaction=transaction,
                )
            if (
                expected_attachments is not None
                and self._current_attachments(page) != expected_attachments
            ):
                return await self._rejected_action(
                    entry,
                    page,
                    action_id=action_id,
                    request=request,
                    failure=BrowserFailure(
                        code="stale_target",
                        message="browser attachment evidence changed after transaction review",
                    ),
                    state=state_before,
                    transaction=transaction,
                )
            backend_request = BackendActionRequest(
                action_id=action_id,
                action=request,
                target=cached,
                expected_preflight=expected_preflight or preflight,
            )
            guard_facts = self._guard_facts(
                _ACTION_TO_TOOL[request.kind],
                entry=entry,
                page=page,
                target_frame_origin=preflight.target.frame_origin,
                effective_destinations=preflight.effective_destinations,
                snapshot_id=target.snapshot_id,
                target_ref=target.ref,
                action_kind=request.kind,
                transaction=transaction,
            )
            await self._reserve_guard(
                "transaction_commits" if request.kind == "commit" else "interactions",
                1,
                guard_facts,
            )
            await self._reserve_guard("navigations", 1, guard_facts)
            await self._reserve_possible_pages(entry, guard_facts)

            before_pages = {item.id: key for key, item in entry.pages_by_key.items()}
            try:
                outcome = await page.handle.perform_action(backend_request)
            except asyncio.CancelledError:
                self._invalidate_snapshot(page)
                interrupted_failure = BrowserFailure(
                    code="action_in_doubt",
                    message=(
                        "browser action was interrupted after dispatch may have begun; "
                        "inspect the page before continuing"
                    ),
                    outcome_uncertain=True,
                )
                page.latest_action = BrowserActionEvidence(
                    action_id=action_id,
                    kind=request.kind,
                    disposition="in_doubt",
                    failure=interrupted_failure,
                    transaction=transaction,
                )
                if request.kind == "commit":
                    page.protected_uses.clear()
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=interrupted_failure,
                )
                raise
            except Exception:
                self._invalidate_snapshot(page)
                failure = BrowserFailure(
                    code="action_in_doubt",
                    message=(
                        "browser action failed after dispatch may have begun; inspect the page "
                        "before continuing"
                    ),
                    outcome_uncertain=True,
                )
                page.latest_action = BrowserActionEvidence(
                    action_id=action_id,
                    kind=request.kind,
                    disposition="in_doubt",
                    failure=failure,
                    transaction=transaction,
                )
                if request.kind == "commit":
                    page.protected_uses.clear()
                result = BrowserActionResult(
                    action_id=action_id,
                    kind=request.kind,
                    disposition="in_doubt",
                    page=self._page_from_state(entry, page, state_before),
                    failure=failure,
                    postcondition=BrowserPostcondition(observation_limited=True),
                    transaction=transaction,
                )
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=failure,
                )
                return result

            if outcome.dispatch_state != "not_dispatched":
                self._invalidate_snapshot(page)
            if outcome.navigation_occurred:
                page.generation += 1
                page.protected_uses.clear()
                if outcome.state_after is not None and not outcome.state_after.closed:
                    page.exact_url = outcome.state_after.url
                    page.title = outcome.state_after.title
            evidence = BrowserActionEvidence(
                action_id=action_id,
                kind=request.kind,
                disposition=outcome.disposition,
                failure=outcome.failure,
                transaction=transaction,
            )
            page.latest_action = evidence
            if request.kind == "commit" and (
                outcome.dispatch_state != "not_dispatched" or outcome.disposition == "in_doubt"
            ):
                page.protected_uses.clear()
            try:
                page_sync = await self._sync_pages(entry, discard_blocked_owned=True)
            except asyncio.CancelledError:
                self._invalidate_snapshot(page)
                page.latest_action = BrowserActionEvidence(
                    action_id=action_id,
                    kind=request.kind,
                    disposition="in_doubt",
                    failure=BrowserFailure(
                        code="action_in_doubt",
                        message=(
                            "browser action completed but post-action observation was interrupted; "
                            "inspect the browser before continuing"
                        ),
                        outcome_uncertain=True,
                    ),
                    transaction=transaction,
                )
                if request.kind == "commit":
                    page.protected_uses.clear()
                latest = page.latest_action
                assert latest is not None and latest.failure is not None
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=latest.failure,
                )
                raise
            except Exception as exc:
                self._invalidate_snapshot(page)
                if isinstance(exc, BrowserError):
                    failure = exc.failure.model_copy(update={"outcome_uncertain": True})
                else:
                    failure = BrowserFailure(
                        code="action_in_doubt",
                        message=(
                            "browser action completed but post-action observation failed; "
                            "inspect the browser before continuing"
                        ),
                        outcome_uncertain=True,
                    )
                evidence = BrowserActionEvidence(
                    action_id=action_id,
                    kind=request.kind,
                    disposition="in_doubt",
                    failure=failure,
                    transaction=transaction,
                )
                page.latest_action = evidence
                known_state = outcome.state_after or outcome.state_before
                result = BrowserActionResult(
                    action_id=action_id,
                    kind=request.kind,
                    disposition="in_doubt",
                    page=self._page_from_state(entry, page, known_state),
                    failure=failure,
                    postcondition=BrowserPostcondition(
                        navigation_occurred=outcome.navigation_occurred,
                        page_closed=known_state.closed,
                        observation_limited=True,
                        observation_note="post-action browser state could not be observed",
                    ),
                    transaction=transaction,
                )
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=failure,
                )
                return result
            if page_sync.blocked_page_count:
                self._invalidate_snapshot(page)
                failure = BrowserFailure(
                    code="destination_blocked",
                    message=("browser action reached a blocked destination; it was not retried"),
                    outcome_uncertain=True,
                )
                evidence = BrowserActionEvidence(
                    action_id=action_id,
                    kind=request.kind,
                    disposition="in_doubt",
                    failure=failure,
                    transaction=transaction,
                )
                page.latest_action = evidence
                known_state = outcome.state_before
                result = BrowserActionResult(
                    action_id=action_id,
                    kind=request.kind,
                    disposition="in_doubt",
                    page=self._page_from_state(entry, page, known_state),
                    failure=failure,
                    postcondition=BrowserPostcondition(
                        navigation_occurred=outcome.navigation_occurred,
                        page_changes=BrowserPageChanges(
                            created_page_ids=page_sync.blocked_page_ids,
                            closed_page_ids=(
                                page_sync.blocked_page_ids if entry.handle.pages_owned else ()
                            ),
                        ),
                        observation_limited=True,
                        observation_note=(
                            "Ricky left blocked external pages open and stopped controlling them"
                            if not entry.handle.pages_owned
                            else "blocked action-created pages were closed"
                        ),
                    ),
                    transaction=transaction,
                )
                await self._record_guard(
                    guard_facts,
                    disposition="in_doubt",
                    action_id=action_id,
                    failure=failure,
                    created_page_count=page_sync.blocked_page_count,
                )
                return result
            after_by_id = {item.id: key for key, item in entry.pages_by_key.items()}
            live_created_ids = tuple(item for item in after_by_id if item not in before_pages)
            existing_closed_ids = tuple(item for item in before_pages if item not in after_by_id)
            created_ids = (*live_created_ids, *page_sync.discarded_page_ids)[:50]
            closed_ids = (*existing_closed_ids, *page_sync.closed_page_ids)[:50]
            selected_popup: str | None = None
            if len(live_created_ids) == 1 and page_sync.discarded_page_count == 0:
                selected_popup = live_created_ids[0]
                entry.selected_page_id = selected_popup
                popup = self._page_entry(entry, selected_popup)
                await popup.handle.bring_to_front()

            result_page = self._result_page(entry, page, selected_popup)
            result_page.latest_action = evidence
            result_state = outcome.state_after
            if (
                result_page is not page
                or result_state is None
                or result_state.key != result_page.handle.key
            ):
                result_state = await result_page.handle.state()
            page_closed = outcome.state_after.closed if outcome.state_after is not None else False
            model = (
                self._page_from_state(entry, page, outcome.state_before)
                if result_state.closed
                else await self._page_model(entry, result_page, state=result_state)
            )
            created_page_count = len(live_created_ids) + page_sync.discarded_page_count
            multiple_popups = created_page_count > 1
            discarded_popups = page_sync.discarded_page_count > 0
            observation_limited = (
                outcome.disposition != "performed"
                or result_state.closed
                or multiple_popups
                or discarded_popups
            )
            fresh: BrowserSnapshot | None = None
            if (
                outcome.dispatch_state != "not_dispatched"
                and not result_state.closed
                and not multiple_popups
                and not discarded_popups
            ):
                try:
                    if result_page is page:
                        fresh = await self._snapshot_locked(entry, result_page)
                    else:
                        async with result_page.lock:
                            fresh = await self._snapshot_locked(entry, result_page)
                    model = fresh.page
                except Exception:
                    observation_limited = True

            result = BrowserActionResult(
                action_id=action_id,
                kind=request.kind,
                disposition=outcome.disposition,
                page=model,
                snapshot=fresh,
                dialogs=outcome.dialogs,
                postcondition=BrowserPostcondition(
                    navigation_occurred=outcome.navigation_occurred,
                    page_closed=page_closed,
                    page_changes=BrowserPageChanges(
                        created_page_ids=created_ids,
                        closed_page_ids=closed_ids,
                        selected_popup_page_id=selected_popup,
                    ),
                    observation_limited=observation_limited,
                    observation_note=(
                        (
                            "action-created pages exceeded the configured page limit; "
                            "Ricky left external pages open and stopped controlling them"
                            if not entry.handle.pages_owned
                            else "action-created pages exceeded the configured page limit "
                            "and were closed; page-change evidence may be truncated"
                        )
                        if discarded_popups
                        else (
                            "multiple popups opened; inspect browser_pages and select one "
                            "explicitly"
                            if multiple_popups
                            else (
                                "request a new snapshot before continuing"
                                if observation_limited
                                else None
                            )
                        )
                    ),
                ),
                failure=outcome.failure,
                transaction=transaction,
            )
            await self._record_guard(
                guard_facts,
                disposition=outcome.disposition,
                action_id=action_id,
                failure=outcome.failure,
                created_page_count=created_page_count,
            )
            return result

    async def handoff(
        self,
        session_id: str,
        *,
        page_id: str | None,
        reason: BrowserHandoffReason,
    ) -> BrowserHandoff:
        """Foreground a headed page and return one fixed trusted user prompt."""

        entry, page = await self._require_page(session_id, page_id)
        if entry.headless is not False:
            raise BrowserError(
                BrowserFailure(
                    code="handoff_required",
                    message="user handoff requires a headed browser session",
                )
            )
        async with page.lock:
            self._ensure_session_active(entry)
            await page.handle.bring_to_front()
            entry.selected_page_id = page.id
            self._invalidate_snapshot(page)
        prompts: dict[BrowserHandoffReason, str] = {
            "captcha": "Complete the CAPTCHA in the headed browser, then reply when ready.",
            "passkey": "Complete the passkey step in the headed browser, then reply when ready.",
            "sso": "Complete the SSO step in the headed browser, then reply when ready.",
            "protected_field": (
                "Complete the protected field locally in the headed browser, then reply when ready."
            ),
            "ambiguous_interface": (
                "Resolve the ambiguous interface in the headed browser, then reply when ready."
            ),
        }
        return BrowserHandoff(
            session_id=entry.id,
            page_id=page.id,
            reason=reason,
            prompt=prompts[reason],
        )

    async def aclose(self) -> None:
        if self._close_complete:
            return
        self._closed = True
        async with self._registry_lock:
            entries = tuple(self._sessions.values())

        async def cleanup() -> None:
            failure: Exception | None = None
            for entry in reversed(entries):
                try:
                    await self._close_entry_when_idle(entry)
                except Exception as exc:  # noqa: BLE001 - finish owned-resource cleanup.
                    failure = failure or exc
                else:
                    async with self._registry_lock:
                        if self._sessions.get(entry.id) is entry:
                            self._sessions.pop(entry.id)
            try:
                await self._backend.aclose()
            except Exception as exc:  # noqa: BLE001 - profile cleanup must still run.
                failure = failure or exc
            else:
                # A backend-wide retry may have completed a handle whose first
                # close attempt was ambiguous. Release only after disconnection
                # is now observable.
                for entry in entries:
                    if entry.guarded_controlled_pages and not entry.handle.connected:
                        guarded_pages = entry.guarded_controlled_pages
                        await self._release_controlled_pages(
                            guarded_pages,
                            self._guard_facts("browser_session_close", entry=entry),
                        )
                        entry.guarded_controlled_pages = 0
                    if entry.lease is not None and not entry.handle.connected:
                        entry.lease.release()
                        entry.lease = None
                        async with self._registry_lock:
                            if self._sessions.get(entry.id) is entry:
                                self._sessions.pop(entry.id)
            if failure is None:
                self._remove_instance_state()
                self._close_complete = True
            if failure is not None:
                raise failure

        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise

    async def _guard_destination(self, url: str) -> None:
        await self._validate_destination(url)

    async def _validate_destination(self, url: str) -> ValidatedDestination:
        validated = await self._policy.validate(url)
        if validated.private:
            self._validated_private_origins.add(validated.origin)
        else:
            self._validated_private_origins.discard(validated.origin)
        return validated

    def _require_screenshot_disclosure(
        self,
        entry: _SessionEntry,
        provider: str,
    ) -> None:
        configured = self._settings.profile_configs.get(entry.resource.profile)
        allowed = (
            configured.browser.screenshot_allowed_providers
            if configured is not None and configured.browser is not None
            else []
        )
        if provider not in allowed:
            raise BrowserError(
                BrowserFailure(
                    code="screenshot_denied",
                    message=(
                        "browser screenshot disclosure is not allowed for the current profile "
                        "and provider"
                    ),
                )
            )

    def _guard_facts(
        self,
        tool_name: BrowserToolName,
        *,
        entry: _SessionEntry | None = None,
        page: _PageEntry | None = None,
        resource: ProfileResourceRef | None = None,
        session_id: str | None = None,
        session_mode: Literal[
            "owned_ephemeral",
            "owned_persistent",
            "attached_cdp",
            "attached_selected_tab",
        ]
        | None = None,
        headless: bool | None = None,
        resource_configuration_digest: str | None = None,
        target_frame_origin: str | None = None,
        effective_destinations: tuple[str, ...] = (),
        provider: str | None = None,
        snapshot_id: str | None = None,
        target_ref: str | None = None,
        action_kind: BrowserActionKind | None = None,
        attachment_count: int = 0,
        attachment_ids: tuple[str, ...] = (),
        attachment_sha256: tuple[str, ...] = (),
        byte_count: int = 0,
        transaction: BrowserTransactionEvidence | None = None,
        coordinate_fallback: CoordinateFallbackEvidence | None = None,
        protected_resource: ProfileResourceRef | None = None,
        protected_revision: int | None = None,
        protected_field: str | None = None,
    ) -> BrowserGuardFacts:
        top_level_origin: str | None = None
        if page is not None and page.exact_url != "about:blank":
            with suppress(ValueError):
                top_level_origin = canonical_origin(page.exact_url)
        destination_origins: list[str] = []
        for destination in effective_destinations:
            with suppress(ValueError):
                origin = canonical_origin(destination)
                if origin not in destination_origins:
                    destination_origins.append(origin)
        return BrowserGuardFacts(
            tool_name=tool_name,
            resource=resource or (entry.resource if entry is not None else None),
            resource_configuration_digest=(
                entry.resource_configuration_digest
                if entry is not None
                else resource_configuration_digest
            ),
            session_id=entry.id if entry is not None else session_id,
            session_mode=entry.mode if entry is not None else session_mode,
            headless=entry.headless if entry is not None else headless,
            page_id=page.id if page is not None else None,
            navigation_generation=page.generation if page is not None else None,
            controlled_page_count=len(entry.pages_by_key) if entry is not None else 0,
            top_level_origin=top_level_origin,
            target_frame_origin=target_frame_origin,
            effective_destination_origins=tuple(destination_origins),
            private_destination_origins=tuple(
                sorted(
                    origin
                    for origin in (
                        top_level_origin,
                        target_frame_origin,
                        *destination_origins,
                    )
                    if origin is not None and origin in self._validated_private_origins
                )
            ),
            provider=provider,
            snapshot_id=snapshot_id,
            target_ref=target_ref,
            action_kind=action_kind,
            attachment_count=attachment_count,
            attachment_ids=attachment_ids,
            attachment_sha256=attachment_sha256,
            byte_count=byte_count,
            transaction=transaction,
            coordinate_fallback=coordinate_fallback,
            protected_resource=protected_resource,
            protected_revision=protected_revision,
            protected_field=protected_field,
        )

    async def _check_guard(self, facts: BrowserGuardFacts) -> None:
        if self._runtime_guard is not None:
            await self._runtime_guard.check(facts)

    async def _reserve_guard(
        self,
        kind: BrowserBudgetKind,
        amount: int,
        facts: BrowserGuardFacts,
    ) -> None:
        if amount < 0:
            raise ValueError("browser budget reservation cannot be negative")
        if self._runtime_guard is not None:
            await self._runtime_guard.check(facts)
            await self._runtime_guard.reserve(kind, amount, facts)

    async def _record_guard(
        self,
        facts: BrowserGuardFacts,
        *,
        disposition: Literal["completed", "not_performed", "performed", "in_doubt"],
        action_id: str | None = None,
        failure: BrowserFailure | None = None,
        result_byte_count: int = 0,
        created_page_count: int = 0,
    ) -> None:
        if self._runtime_guard is not None:
            await self._runtime_guard.record(
                BrowserRuntimeEvidence(
                    facts=facts,
                    disposition=disposition,
                    action_id=action_id,
                    failure=failure,
                    result_byte_count=result_byte_count,
                    created_page_count=created_page_count,
                )
            )

    async def _reserve_possible_pages(
        self,
        entry: _SessionEntry,
        facts: BrowserGuardFacts,
    ) -> None:
        if self._runtime_guard is not None:
            await self._runtime_guard.check(facts)
            # One dispatch can synchronously create pages until the local
            # browser ceiling is reached. Reserve that worst case before work;
            # passing one at the ceiling makes the guard deny an operation that
            # could otherwise exceed its simultaneous-page allowance.
            remaining = self._runtime_guard.controlled_page_ceiling - facts.controlled_page_count
            await self._runtime_guard.reserve_possible_pages(
                max(1, remaining),
                facts,
            )
            entry.guarded_controlled_pages += max(1, remaining)

    async def _release_controlled_pages(
        self,
        amount: int,
        facts: BrowserGuardFacts,
    ) -> None:
        if amount < 0:
            raise ValueError("controlled-page release cannot be negative")
        if amount and self._runtime_guard is not None:
            operation = asyncio.create_task(
                self._runtime_guard.release_controlled_pages(amount, facts)
            )
            interrupted = False
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    interrupted = True
                except Exception:
                    break
            operation.result()
            if interrupted:
                raise asyncio.CancelledError

    def _resource_model(self, resolved: ResolvedBrowserResource) -> BrowserResource:
        lease = BrowserResourceLease(self._settings, resolved.ref)
        persistent = isinstance(resolved.settings, PersistentBrowserResourceSettings)
        available = os.name == "posix" and (not persistent or self._executable_path.is_file())
        busy = lease.is_active() if available else False
        return BrowserResource(
            resource=resolved.ref,
            kind=resolved.settings.kind,
            description=resolved.settings.description,
            availability=("busy" if busy else "available" if available else "unavailable"),
            headless=(
                resolved.settings.headless
                if isinstance(resolved.settings, PersistentBrowserResourceSettings)
                else None
            ),
            process_owned=persistent,
        )

    def _require_resource(self, qualified: str) -> ResolvedBrowserResource:
        self._ensure_open()
        try:
            ref = ProfileResourceRef.from_qualified(qualified)
        except ValueError as exc:
            raise BrowserError(
                BrowserFailure(
                    code="unknown_resource",
                    message="unknown or inaccessible configured browser resource",
                )
            ) from exc
        resolved = require_browser_resource(self._settings, scope=self._scope, ref=ref)
        if resolved is None:
            raise BrowserError(
                BrowserFailure(
                    code="unknown_resource",
                    message="unknown or inaccessible configured browser resource",
                )
            )
        return resolved

    def _prepare_persistent_state(self, ref: ProfileResourceRef) -> Path:
        ensure_private_user_data_root(self._settings)
        path = persistent_browser_path(self._settings, ref)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = persistent_browser_path(self._settings, ref)
        if current != path:
            raise ValueError("persistent browser path changed before creation")
        current.mkdir(exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(current.parent, 0o700)
            os.chmod(current, 0o700)
        return current

    def _confined_state_path(self, *parts: str) -> Path:
        relative = PurePosixPath(self._settings.browser.ephemeral_dir, *parts).as_posix()
        return profile_data_subpath(self._settings, self._scope.primary, relative)

    def _remove_instance_state(self) -> None:
        if not self._instance_state_created:
            return
        try:
            current = self._confined_state_path(self._instance_id)
        except ValueError:
            return
        if current != self._instance_dir:
            return
        shutil.rmtree(current, ignore_errors=True)
        self._instance_state_created = False
        _remove_empty_parents(
            current.parent,
            stop=profile_data_path(self._settings, self._scope.primary),
        )

    async def _snapshot_locked(
        self,
        entry: _SessionEntry,
        page: _PageEntry,
    ) -> BrowserSnapshot:
        self._ensure_session_active(entry)
        state_before = await page.handle.state()
        if state_before.closed:
            raise BrowserError(BrowserFailure(code="page_closed", message="browser page is closed"))
        await self._page_model(entry, page, state=state_before)
        guard_facts = self._guard_facts(
            "browser_snapshot",
            entry=entry,
            page=page,
        )
        await self._reserve_guard("semantic_observations", 1, guard_facts)
        backend_snapshot = await page.handle.snapshot(
            depth=self._settings.browser.snapshot_depth,
            character_limit=self._settings.browser.snapshot_char_limit,
        )
        raw = sanitize_aria_snapshot(
            backend_snapshot.content,
            protected_refs=(
                target.ref for target in backend_snapshot.targets if target.protected or target.file
            ),
        )
        state_after = await page.handle.state()
        if state_after.closed:
            raise BrowserError(
                BrowserFailure(
                    code="page_closed",
                    message="browser page closed during snapshot",
                )
            )
        if state_after.url != state_before.url:
            page.exact_url = state_after.url
            page.title = state_after.title
            page.generation += 1
            page.protected_uses.clear()
            self._invalidate_snapshot(page)
            raise BrowserError(
                BrowserFailure(
                    code="backend_error",
                    message="browser page navigated during snapshot; request a new snapshot",
                    retryable=True,
                )
            )
        limit = self._settings.browser.snapshot_char_limit
        truncated = backend_snapshot.character_truncated or len(raw) > limit
        content = raw[:limit]
        snapshot_id = f"browser_snapshot_{uuid.uuid4().hex}"
        refs = tuple(dict.fromkeys(_REF.findall(content)))
        grouped: dict[str, list[BackendTargetDescriptor]] = {ref: [] for ref in refs}
        for descriptor in backend_snapshot.targets:
            if descriptor.ref in grouped:
                grouped[descriptor.ref].append(descriptor)
        page.snapshot_id = snapshot_id
        page.targets_by_ref = {ref: tuple(grouped[ref]) for ref in refs}
        page.visual = None
        page.semantic = _SemanticEntry(
            snapshot_id=snapshot_id,
            navigation_generation=page.generation,
            targets_by_ref=dict(page.targets_by_ref),
        )
        page.semantic_preflight_failure = None
        model = await self._page_model(entry, page, state=state_after)
        targets = tuple(
            BrowserTarget(
                ref=ref,
                session_id=entry.id,
                page_id=page.id,
                navigation_generation=page.generation,
                snapshot_id=snapshot_id,
            )
            for ref in refs
        )
        descriptors = tuple(
            values[0].provider_descriptor()
            for ref in refs
            if len(values := page.targets_by_ref[ref]) == 1
        )
        result = BrowserSnapshot(
            snapshot_id=snapshot_id,
            page=model,
            content=content,
            targets=targets,
            descriptors=descriptors,
            depth_limit=self._settings.browser.snapshot_depth,
            character_limit=limit,
            character_truncated=truncated,
        )
        await self._record_guard(guard_facts, disposition="completed")
        return result

    async def _rejected_action(
        self,
        entry: _SessionEntry,
        page: _PageEntry,
        *,
        action_id: str,
        request: BrowserActionRequest,
        failure: BrowserFailure,
        state: BackendPageState,
        transaction: BrowserTransactionEvidence | None = None,
    ) -> BrowserActionResult:
        evidence = BrowserActionEvidence(
            action_id=action_id,
            kind=request.kind,
            disposition="not_performed",
            failure=failure,
            transaction=transaction,
        )
        page.latest_action = evidence
        result = BrowserActionResult(
            action_id=action_id,
            kind=request.kind,
            disposition="not_performed",
            page=self._page_from_state(entry, page, state),
            failure=failure,
            transaction=transaction,
        )
        await self._record_guard(
            self._guard_facts(
                _ACTION_TO_TOOL[request.kind],
                entry=entry,
                page=page,
                action_kind=request.kind,
                transaction=transaction,
            ),
            disposition="not_performed",
            action_id=action_id,
            failure=failure,
        )
        return result

    async def _protected_rejection(
        self,
        entry: _SessionEntry,
        page: _PageEntry,
        action_id: str,
        material: ProtectedMaterial,
        failure: BrowserFailure,
        state: BackendPageState,
    ) -> BrowserActionResult:
        evidence = BrowserActionEvidence(
            action_id=action_id,
            kind="protected_fill",
            disposition="not_performed",
            failure=failure,
            protected_ref=material.descriptor.ref,
            protected_field=material.field.name,
        )
        page.latest_action = evidence
        result = BrowserActionResult(
            action_id=action_id,
            kind="protected_fill",
            disposition="not_performed",
            page=self._page_from_state(entry, page, state),
            failure=failure,
        )
        await self._record_guard(
            self._guard_facts(
                "browser_fill_protected",
                entry=entry,
                page=page,
                action_kind="protected_fill",
                protected_resource=material.descriptor.ref,
                protected_field=material.field.name,
            ),
            disposition="not_performed",
            action_id=action_id,
            failure=failure,
        )
        return result

    @staticmethod
    def _record_interrupted_effect(
        page: _PageEntry,
        action_id: str,
        kind: Literal[
            "upload",
            "download",
            "coordinate_click",
            "coordinate_commit",
            "protected_fill",
        ],
        *,
        protected_ref: ProfileResourceRef | None = None,
        protected_field: str | None = None,
        transaction: BrowserTransactionEvidence | None = None,
    ) -> None:
        BrowserService._invalidate_snapshot(page)
        page.latest_action = BrowserActionEvidence(
            action_id=action_id,
            kind=kind,
            disposition="in_doubt",
            failure=BrowserFailure(
                code="action_in_doubt",
                message="browser effect was interrupted after dispatch may have begun",
                outcome_uncertain=True,
            ),
            protected_ref=protected_ref,
            protected_field=protected_field,
            transaction=transaction,
        )
        if transaction is not None or kind == "coordinate_commit":
            page.protected_uses.clear()

    async def _finalize_special_action(
        self,
        entry: _SessionEntry,
        page: _PageEntry,
        *,
        action_id: str,
        kind: Literal[
            "upload",
            "coordinate_click",
            "coordinate_commit",
            "protected_fill",
        ],
        outcome: BackendActionOutcome,
        protected_ref: ProfileResourceRef | None = None,
        protected_field: str | None = None,
        transaction: BrowserTransactionEvidence | None = None,
        before_page_ids: frozenset[str],
    ) -> BrowserActionResult:
        if outcome.dispatch_state != "not_dispatched":
            self._invalidate_snapshot(page)
        if outcome.navigation_occurred:
            page.generation += 1
            page.protected_uses.clear()
        state = outcome.state_after or outcome.state_before
        if not state.closed:
            page.exact_url = state.url
            page.title = state.title
        page_sync = _PageSyncResult()
        if outcome.disposition == "performed":
            try:
                page_sync = await self._sync_pages(entry, discard_blocked_owned=True)
            except asyncio.CancelledError:
                self._record_interrupted_effect(
                    page,
                    action_id,
                    kind,
                    protected_ref=protected_ref,
                    protected_field=protected_field,
                    transaction=transaction,
                )
                raise
            except Exception as exc:
                failure = (
                    exc.failure.model_copy(update={"outcome_uncertain": True})
                    if isinstance(exc, BrowserError)
                    else BrowserFailure(
                        code="action_in_doubt",
                        message=(
                            "browser effect completed but post-action observation failed; "
                            "inspect the browser before continuing"
                        ),
                        outcome_uncertain=True,
                    )
                )
                outcome = BackendActionOutcome(
                    disposition="in_doubt",
                    dispatch_state="dispatched",
                    state_before=outcome.state_before,
                    state_after=outcome.state_after,
                    navigation_occurred=outcome.navigation_occurred,
                    dialogs=outcome.dialogs,
                    failure=failure,
                )
            else:
                if page_sync.blocked_page_count:
                    outcome = BackendActionOutcome(
                        disposition="in_doubt",
                        dispatch_state="dispatched",
                        state_before=outcome.state_before,
                        state_after=outcome.state_after,
                        navigation_occurred=outcome.navigation_occurred,
                        dialogs=outcome.dialogs,
                        failure=BrowserFailure(
                            code="destination_blocked",
                            message=(
                                "browser effect reached a blocked destination; it was not retried"
                            ),
                            outcome_uncertain=True,
                        ),
                    )
        evidence = BrowserActionEvidence(
            action_id=action_id,
            kind=kind,
            disposition=outcome.disposition,
            failure=outcome.failure,
            protected_ref=protected_ref,
            protected_field=protected_field,
            transaction=transaction,
        )
        page.latest_action = evidence
        if kind == "coordinate_commit" and (
            outcome.dispatch_state != "not_dispatched" or outcome.disposition == "in_doubt"
        ):
            page.protected_uses.clear()
        after_page_ids = {item.id for item in entry.pages_by_key.values()}
        live_created_ids = tuple(sorted(after_page_ids - before_page_ids))
        created_ids = tuple(
            dict.fromkeys(
                (
                    *live_created_ids,
                    *page_sync.discarded_page_ids,
                    *page_sync.blocked_page_ids,
                )
            )
        )[:50]
        closed_ids = tuple(
            dict.fromkeys(
                (
                    *sorted(before_page_ids - after_page_ids),
                    *page_sync.closed_page_ids,
                )
            )
        )[:50]
        return BrowserActionResult(
            action_id=action_id,
            kind=kind,
            disposition=outcome.disposition,
            page=self._page_from_state(entry, page, state),
            dialogs=outcome.dialogs,
            postcondition=BrowserPostcondition(
                navigation_occurred=outcome.navigation_occurred,
                page_closed=state.closed,
                page_changes=BrowserPageChanges(
                    created_page_ids=created_ids,
                    closed_page_ids=closed_ids,
                ),
                observation_limited=True,
                observation_note="request a new snapshot before continuing",
            ),
            failure=outcome.failure,
            transaction=transaction,
        )

    def _publish_download(
        self,
        entry: _SessionEntry,
        temporary_path: Path,
        suggested_filename: str,
        media_type: str | None,
    ) -> BrowserDownloadRef:
        attempt_root = entry.download_temp_dir
        if attempt_root is None:
            raise ValueError("browser download has no owned attempt root")
        if temporary_path.is_symlink() or not temporary_path.resolve().is_relative_to(
            attempt_root.resolve()
        ):
            raise ValueError("browser download temporary path escaped its owner")
        metadata = temporary_path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("browser download temporary body is not a regular file")
        if metadata.st_size > self._settings.browser.download_file_byte_limit:
            raise _DownloadPublicationTooLarge(
                "browser download exceeds the configured publication limit"
            )
        digest = hashlib.sha256()
        observed = 0
        with temporary_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                observed += len(chunk)
                if observed > self._settings.browser.download_file_byte_limit:
                    raise _DownloadPublicationTooLarge(
                        "browser download exceeds the configured publication limit"
                    )
                digest.update(chunk)
        if observed != metadata.st_size:
            raise ValueError("browser download changed while being inspected")
        filename = _safe_download_filename(suggested_filename)
        reference = BrowserDownloadRef(
            id=f"browser_download_{uuid.uuid4().hex}",
            profile=entry.resource.profile,
            filename=filename,
            media_type=(
                media_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            ),
            size_bytes=observed,
            sha256=digest.hexdigest(),
        )
        final = browser_download_path(self._settings, reference)
        root = final.parent
        ensure_private_user_data_root(self._settings)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or final.exists() or final.is_symlink():
            raise ValueError("browser download destination is unsafe or already exists")
        descriptor, staged_name = tempfile.mkstemp(prefix=".download-", dir=root)
        staged = Path(staged_name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as destination, temporary_path.open("rb") as source:
                descriptor = -1
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            os.link(staged, final)
            if os.name == "posix":
                final.chmod(0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            staged.unlink(missing_ok=True)
        return reference

    @staticmethod
    def _validate_action(
        target: BackendTargetDescriptor,
        request: BrowserActionRequest,
    ) -> BrowserFailure | None:
        if target.file:
            return BrowserFailure(
                code="file_control",
                message="browser file controls require the prepared browser_upload tool",
            )
        if target.disabled:
            return BrowserFailure(
                code="incompatible_target",
                message="browser target is disabled; request a new snapshot",
            )
        if request.kind == "fill":
            if target.protected:
                return BrowserFailure(
                    code="protected_field",
                    message=(
                        "recognized credential or payment fields require local user handoff "
                        "until protected values are supported"
                    ),
                )
            if not target.editable or target.control_kind not in {
                "text",
                "search",
                "email",
                "telephone",
                "url",
                "date",
                "number",
                "textarea",
                "contenteditable",
            }:
                return BrowserFailure(
                    code="incompatible_target",
                    message="browser target does not support ordinary text entry",
                )
        elif request.kind == "select":
            if target.control_kind != "select":
                return BrowserFailure(
                    code="incompatible_target",
                    message="browser target is not a select control",
                )
            if target.option_labels and request.option_label not in target.option_labels:
                return BrowserFailure(
                    code="incompatible_target",
                    message="selected option is not present; request a new snapshot",
                )
            if target.option_labels.count(request.option_label) > 1:
                return BrowserFailure(
                    code="ambiguous_target",
                    message="selected option label is ambiguous; request user guidance",
                )
        elif request.kind == "set_checked":
            if target.control_kind not in {"checkbox", "radio"}:
                return BrowserFailure(
                    code="incompatible_target",
                    message="browser target is not a checkbox or radio control",
                )
            if target.control_kind == "radio" and request.checked is False:
                return BrowserFailure(
                    code="incompatible_target",
                    message="a radio control cannot be unchecked directly",
                )
        elif request.kind == "click":
            if target.consequential:
                return BrowserFailure(
                    code="consequential_target",
                    message="recognized consequential target requires browser_commit",
                )
            if target.control_kind in {"checkbox", "radio", "select"}:
                return BrowserFailure(
                    code="incompatible_target",
                    message="use the specialized browser control action for this target",
                )
        elif request.kind == "press_key" and target.protected:
            return BrowserFailure(
                code="protected_field",
                message="recognized protected fields require local user handoff",
            )
        return None

    @staticmethod
    def _validate_protected_target(
        target: BackendTargetDescriptor,
    ) -> BrowserFailure | None:
        if (
            not target.protected
            or target.protected_kind is None
            or not target.editable
            or target.file
            or target.disabled
        ):
            return BrowserFailure(
                code="incompatible_target",
                message="browser target is not a supported editable protected control",
            )
        return None

    @staticmethod
    def _invalidate_snapshot(page: _PageEntry) -> None:
        page.snapshot_id = None
        page.targets_by_ref.clear()
        page.visual = None
        page.semantic = None
        page.semantic_preflight_failure = None

    @staticmethod
    def _cached_target(
        page: _PageEntry,
        target: BrowserActionTarget,
    ) -> BackendTargetDescriptor:
        if page.snapshot_id is None or target.snapshot_id != page.snapshot_id:
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="browser target snapshot is stale; request a new snapshot",
                )
            )
        candidates = page.targets_by_ref.get(target.ref)
        if candidates is None or len(candidates) == 0:
            raise BrowserError(
                BrowserFailure(
                    code="invented_target",
                    message="browser target ref was not issued by the current snapshot",
                )
            )
        if len(candidates) != 1:
            raise BrowserError(
                BrowserFailure(
                    code="ambiguous_target",
                    message="browser target is ambiguous; use user handoff",
                )
            )
        return candidates[0]

    @staticmethod
    def _page_entry(entry: _SessionEntry, page_id: str) -> _PageEntry:
        for page in entry.pages_by_key.values():
            if page.id == page_id:
                return page
        raise BrowserError(BrowserFailure(code="unknown_page", message="unknown browser page id"))

    @staticmethod
    def _result_page(
        entry: _SessionEntry,
        original: _PageEntry,
        selected_popup: str | None,
    ) -> _PageEntry:
        if selected_popup is not None:
            return BrowserService._page_entry(entry, selected_popup)
        if original in entry.pages_by_key.values():
            return original
        if entry.selected_page_id is not None:
            return BrowserService._page_entry(entry, entry.selected_page_id)
        return original

    async def _sync_pages(
        self,
        entry: _SessionEntry,
        *,
        discard_blocked_owned: bool = False,
    ) -> _PageSyncResult:
        async with entry.sync_lock:
            self._ensure_session_active(entry)
            handles = await entry.handle.pages()
            discarded_page_ids: list[str] = []
            closed_page_ids: list[str] = []
            discarded_page_count = entry.handle.take_page_overflow_count()
            unsupported_page_count = 0
            blocked_page_ids: list[str] = []
            blocked_page_count = 0
            for _index in range(min(discarded_page_count, 50)):
                discarded = f"browser_page_{uuid.uuid4().hex}"
                discarded_page_ids.append(discarded)
                if entry.handle.pages_owned:
                    closed_page_ids.append(discarded)
            live_keys: set[str] = set()
            eligible: list[tuple[BrowserPageHandle, BackendPageState]] = []
            for handle in handles:
                state = await handle.state()
                if state.closed:
                    continue
                enforce_policy = not entry.handle.pages_owned or discard_blocked_owned
                if enforce_policy and not _supported_attached_url(state.url):
                    unsupported_page_count += 1
                    blocked_page_count += 1
                    if len(blocked_page_ids) < 50:
                        blocked_page_ids.append(f"browser_page_{uuid.uuid4().hex}")
                    await handle.close()
                    continue
                if enforce_policy and state.url != "about:blank":
                    try:
                        await self._validate_destination(state.url)
                    except BrowserError:
                        unsupported_page_count += 1
                        blocked_page_count += 1
                        if len(blocked_page_ids) < 50:
                            blocked_page_ids.append(f"browser_page_{uuid.uuid4().hex}")
                        await handle.close()
                        continue
                live_keys.add(handle.key)
                eligible.append((handle, state))
            for key in tuple(entry.pages_by_key):
                if key not in live_keys:
                    entry.pages_by_key.pop(key)
            for handle, state in eligible:
                if handle.key in entry.pages_by_key:
                    continue
                page_limit = (
                    min(
                        self._settings.browser.max_pages,
                        entry.guarded_controlled_pages,
                    )
                    if self._runtime_guard is not None
                    else self._settings.browser.max_pages
                )
                if len(entry.pages_by_key) >= page_limit:
                    discarded_page_count += 1
                    if len(discarded_page_ids) < 50:
                        discarded = f"browser_page_{uuid.uuid4().hex}"
                        discarded_page_ids.append(discarded)
                        if entry.handle.pages_owned:
                            closed_page_ids.append(discarded)
                    # Closing an attached handle means quarantine at the backend
                    # boundary; it never closes the user-owned external tab.
                    await handle.close()
                    continue
                page = _PageEntry(
                    id=f"browser_page_{uuid.uuid4().hex}",
                    handle=handle,
                    exact_url=state.url,
                    title=state.title,
                )
                entry.pages_by_key[handle.key] = page
            if entry.selected_page_id not in {page.id for page in entry.pages_by_key.values()}:
                entry.selected_page_id = next(
                    (page.id for page in entry.pages_by_key.values()),
                    None,
                )
            actual_controlled_pages = len(entry.pages_by_key)
            if entry.guarded_controlled_pages > actual_controlled_pages:
                await self._release_controlled_pages(
                    entry.guarded_controlled_pages - actual_controlled_pages,
                    self._guard_facts("browser_pages", entry=entry),
                )
                entry.guarded_controlled_pages = actual_controlled_pages
            return _PageSyncResult(
                discarded_page_ids=tuple(discarded_page_ids),
                closed_page_ids=tuple(closed_page_ids),
                blocked_page_ids=tuple(blocked_page_ids),
                discarded_page_count=discarded_page_count,
                unsupported_page_count=unsupported_page_count,
                blocked_page_count=blocked_page_count,
            )

    async def _session_model(self, entry: _SessionEntry) -> BrowserSession:
        pages = await self.pages(entry.id)
        return BrowserSession(
            session_id=entry.id,
            resource=entry.resource,
            mode=entry.mode,
            headless=entry.headless,
            process_owned=entry.handle.process_owned,
            selected_page_id=pages.selected_page_id,
            pages=pages.pages,
        )

    async def _page_model(
        self,
        entry: _SessionEntry,
        page: _PageEntry,
        *,
        state: object | None = None,
    ) -> BrowserPage:
        current = state if isinstance(state, BackendPageState) else await page.handle.state()
        if current.closed:
            raise BrowserError(BrowserFailure(code="page_closed", message="browser page is closed"))
        if current.url != "about:blank":
            await self._validate_destination(current.url)
        if current.url != page.exact_url:
            page.exact_url = current.url
            page.generation += 1
            page.protected_uses.clear()
            self._invalidate_snapshot(page)
        page.title = current.title
        return self._page_from_state(entry, page, current)

    def _page_from_state(
        self,
        entry: _SessionEntry,
        page: _PageEntry,
        state: BackendPageState,
    ) -> BrowserPage:
        current_url = page.exact_url if state.closed else state.url
        current_title = page.title if state.closed else state.title
        origin = None
        if current_url != "about:blank":
            try:
                origin = canonical_origin(current_url)
            except ValueError:
                origin = None
        return BrowserPage(
            session_id=entry.id,
            page_id=page.id,
            selected=entry.selected_page_id == page.id,
            url=provider_safe_url(current_url),
            origin=origin,
            title=current_title[:1_000],
            navigation_generation=page.generation,
            latest_action=page.latest_action,
        )

    async def _require_page(
        self,
        session_id: str,
        page_id: str | None,
    ) -> tuple[_SessionEntry, _PageEntry]:
        entry = self._require_session(session_id)
        await self._sync_pages(entry)
        selected = page_id or entry.selected_page_id
        for page in entry.pages_by_key.values():
            if page.id == selected:
                return entry, page
        raise BrowserError(BrowserFailure(code="unknown_page", message="unknown browser page id"))

    def _require_session(self, session_id: str) -> _SessionEntry:
        self._ensure_open()
        entry = self._sessions.get(session_id)
        if entry is None:
            raise BrowserError(
                BrowserFailure(code="unknown_session", message="unknown browser session id")
            )
        self._ensure_session_active(entry)
        return entry

    def _ensure_open(self) -> None:
        if self._closed:
            raise BrowserError(
                BrowserFailure(code="session_closed", message="browser runtime is closed")
            )

    @staticmethod
    def _ensure_session_active(entry: _SessionEntry) -> None:
        if entry.closing:
            raise BrowserError(
                BrowserFailure(code="session_closed", message="browser session is closing")
            )
        if entry.mode == "attached_cdp" and not entry.handle.connected:
            for page in entry.pages_by_key.values():
                BrowserService._invalidate_snapshot(page)
            raise BrowserError(
                BrowserFailure(
                    code="attachment_disconnected",
                    message="configured browser attachment disconnected",
                )
            )
        if entry.cleanup_failed:
            raise BrowserError(
                BrowserFailure(
                    code="backend_error",
                    message="browser session cleanup failed and must be retried",
                )
            )

    async def _close_entry(self, entry: _SessionEntry) -> None:
        await entry.handle.close()
        if entry.handle.connected:
            raise BrowserError(
                BrowserFailure(
                    code="backend_error",
                    message="browser cleanup could not confirm process or connection closure",
                )
            )
        if entry.mode == "owned_ephemeral" and entry.state_dir is not None:
            shutil.rmtree(entry.state_dir, ignore_errors=True)
        elif entry.download_temp_dir is not None:
            shutil.rmtree(entry.download_temp_dir.parent, ignore_errors=True)
        if entry.lease is not None:
            entry.lease.release()
            entry.lease = None
        if entry.guarded_controlled_pages:
            guarded_pages = entry.guarded_controlled_pages
            await self._release_controlled_pages(
                guarded_pages,
                self._guard_facts("browser_session_close", entry=entry),
            )
            entry.guarded_controlled_pages = 0

    async def _rollback_open_session(self, entry: _SessionEntry) -> None:
        """Remove and close a session whose initial state never became observable."""

        cleanup = asyncio.create_task(self._close_entry_when_idle(entry))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            async with self._registry_lock:
                if self._sessions.get(entry.id) is entry:
                    self._sessions.pop(entry.id)
            raise
        except Exception:
            # Preserve the stronger initial-construction failure after exhausting cleanup.
            pass
        else:
            async with self._registry_lock:
                if self._sessions.get(entry.id) is entry:
                    self._sessions.pop(entry.id)

    async def _close_entry_when_idle(self, entry: _SessionEntry) -> None:
        async with entry.action_lock:
            pages = tuple(sorted(entry.pages_by_key.values(), key=lambda page: page.id))
            acquired: list[asyncio.Lock] = []
            try:
                for page in pages:
                    await page.lock.acquire()
                    acquired.append(page.lock)
                entry.closing = True
                try:
                    await self._close_entry(entry)
                except BaseException:
                    entry.cleanup_failed = True
                    entry.closing = False
                    raise
                entry.cleanup_failed = False
            finally:
                for lock in reversed(acquired):
                    lock.release()


def _remove_empty_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _safe_download_filename(value: str) -> str:
    """Return a bounded plain filename without trusting server path syntax."""
    raw = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "download"
    stem = cleaned[:180]
    return stem or "download"


def _supported_attached_url(url: str) -> bool:
    if url == "about:blank":
        return True
    parsed = urlsplit(url)
    return parsed.scheme.casefold() in {"http", "https"} and parsed.hostname is not None


def _exact_transaction_origins(
    top_level_origin: str | None,
    target_frame_origin: str | None,
) -> tuple[str, str]:
    if top_level_origin is None or target_frame_origin is None:
        raise BrowserError(
            BrowserFailure(
                code="transaction_envelope",
                message=(
                    "browser transaction review requires exact top-level and target-frame origins"
                ),
            )
        )
    return top_level_origin, target_frame_origin


def _attachment_timeout() -> BrowserError:
    return BrowserError(
        BrowserFailure(
            code="attachment_timeout",
            message="configured browser attachment timed out",
            retryable=True,
        )
    )
