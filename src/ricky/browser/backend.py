"""Private browser backend and live-handle protocols."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import SecretStr

from ricky.browser.types import (
    BrowserActionRequest,
    BrowserControlKind,
    BrowserDialogObservation,
    BrowserEffectDisposition,
    BrowserFailure,
    BrowserTargetDescriptor,
)
from ricky.protected_values import ProtectedControlKind

type DestinationGuard = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class BrowserLaunchOptions:
    mode: Literal["owned_ephemeral", "owned_persistent"]
    user_data_dir: Path
    download_temp_dir: Path
    executable_path: Path
    headless: bool
    navigation_timeout_ms: float
    operation_timeout_ms: float
    max_redirects: int
    page_discovery_limit: int
    download_file_byte_limit: int = 50_000_000


@dataclass(frozen=True)
class BrowserCdpOptions:
    mode: Literal["attached_cdp"]
    endpoint: str
    attachment_timeout_ms: float
    navigation_timeout_ms: float
    operation_timeout_ms: float
    max_redirects: int
    page_discovery_limit: int


type BrowserOpenOptions = BrowserLaunchOptions | BrowserCdpOptions


@dataclass(frozen=True)
class BackendPageState:
    key: str
    url: str
    title: str
    closed: bool = False


@dataclass(frozen=True)
class BackendTargetDescriptor:
    """Backend-issued target facts with one private frame identity."""

    ref: str
    role: str = ""
    name: str = ""
    control_kind: BrowserControlKind = "other"
    frame_origin: str | None = None
    frame_key: str = "main"
    checked: bool | None = None
    disabled: bool = False
    editable: bool = False
    option_labels: tuple[str, ...] = ()
    consequential: bool = False
    protected: bool = False
    protected_kind: ProtectedControlKind | None = None
    file: bool = False
    multiple: bool = False
    accept: tuple[str, ...] = ()
    restricted_interaction: Literal["captcha", "passkey", "sso"] | None = None

    def provider_descriptor(self) -> BrowserTargetDescriptor:
        """Drop private backend identity before returning target facts to a provider."""

        return BrowserTargetDescriptor(
            ref=self.ref,
            role=self.role,
            name=self.name,
            control_kind=self.control_kind,
            frame_origin=self.frame_origin,
            checked=self.checked,
            disabled=self.disabled,
            editable=self.editable,
            option_labels=self.option_labels,
            consequential=self.consequential,
            protected=self.protected,
            protected_kind=self.protected_kind,
            file=self.file,
            multiple=self.multiple,
            accept=self.accept,
        )


@dataclass(frozen=True)
class BackendActionPreflight:
    """Exact live semantic target and locally resolved destination facts."""

    target: BackendTargetDescriptor
    effective_destinations: tuple[str, ...] = ()
    financial_signal: bool = False


@dataclass(frozen=True)
class BackendCoordinatePreflight:
    """Exact live coordinate hit target and locally resolved destination facts."""

    target: BackendTargetDescriptor
    effective_destinations: tuple[str, ...] = ()
    financial_signal: bool = False
    equivalent_semantic_ref: str | None = None


@dataclass(frozen=True)
class BackendSnapshot:
    content: str
    targets: tuple[BackendTargetDescriptor, ...] = ()
    character_truncated: bool = False


@dataclass(frozen=True)
class BackendBoundingBox:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class BackendViewport:
    width: int
    height: int
    scroll_x: float
    scroll_y: float
    device_scale_factor: float


@dataclass(frozen=True)
class BackendVisualCandidate:
    descriptor: BackendTargetDescriptor
    bounding_box: BackendBoundingBox


@dataclass(frozen=True)
class BackendVisualSnapshot:
    png: bytes
    masked_base_sha256: str
    viewport: BackendViewport
    candidates: tuple[BackendVisualCandidate, ...]
    candidate_truncated: bool = False


@dataclass(frozen=True)
class BackendUploadFile:
    filename: str
    media_type: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class BackendDownload:
    temporary_path: Path
    suggested_filename: str
    media_type: str | None = None


@dataclass(frozen=True)
class BackendDownloadOutcome:
    action: BackendActionOutcome
    download: BackendDownload | None = None


@dataclass(frozen=True)
class BackendCoordinateRequest:
    action_id: str
    x: float
    y: float
    masked_base_sha256: str
    viewport: BackendViewport
    dialog_response: Literal["dismiss", "accept"] = "dismiss"
    dialog_prompt_text: str | None = None


@dataclass(frozen=True)
class BackendActionRequest:
    """One backend action attempt, already resolved against cached target facts."""

    action_id: str
    action: BrowserActionRequest
    target: BackendTargetDescriptor
    expected_preflight: BackendActionPreflight | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"browser_action_[0-9a-f]{32}", self.action_id) is None:
            raise ValueError("backend action id must be opaque")
        if self.expected_preflight is not None and self.expected_preflight.target != self.target:
            raise ValueError("backend expected preflight must describe the request target")


@dataclass(frozen=True, repr=False)
class BackendProtectedFillRequest:
    """One in-process-only protected fill; never serialized or provider-visible."""

    action_id: str
    target: BackendTargetDescriptor
    value: SecretStr

    def __post_init__(self) -> None:
        if re.fullmatch(r"browser_action_[0-9a-f]{32}", self.action_id) is None:
            raise ValueError("backend protected-fill action id must be opaque")


BackendDispatchState = Literal["not_dispatched", "dispatched", "completed"]


@dataclass(frozen=True)
class BackendActionOutcome:
    """Browser-level evidence from a single, never-replayed dispatch attempt."""

    disposition: BrowserEffectDisposition
    dispatch_state: BackendDispatchState
    state_before: BackendPageState
    state_after: BackendPageState | None = None
    navigation_occurred: bool = False
    dialogs: tuple[BrowserDialogObservation, ...] = ()
    failure: BrowserFailure | None = None

    def __post_init__(self) -> None:
        expected_dispatch = {
            "not_performed": "not_dispatched",
            "performed": "completed",
            "in_doubt": "dispatched",
        }
        if self.dispatch_state != expected_dispatch[self.disposition]:
            raise ValueError("backend disposition and dispatch state are inconsistent")


class BrowserPageHandle(Protocol):
    @property
    def key(self) -> str: ...

    async def state(self) -> BackendPageState: ...

    async def bring_to_front(self) -> None: ...

    async def navigate(self, url: str) -> BackendPageState: ...

    async def scroll(self, delta_y: int) -> BackendPageState: ...

    async def snapshot(self, *, depth: int, character_limit: int) -> BackendSnapshot: ...

    async def visual_snapshot(self, *, candidate_limit: int) -> BackendVisualSnapshot: ...

    async def preflight_action(
        self,
        request: BackendActionRequest,
    ) -> BackendActionPreflight: ...

    async def preflight_protected_target(
        self,
        target: BackendTargetDescriptor,
    ) -> BackendTargetDescriptor: ...

    async def perform_action(
        self,
        request: BackendActionRequest,
    ) -> BackendActionOutcome: ...

    async def perform_protected_fill(
        self,
        request: BackendProtectedFillRequest,
    ) -> BackendActionOutcome: ...

    async def perform_upload(
        self,
        request: BackendActionRequest,
        files: tuple[BackendUploadFile, ...],
    ) -> BackendActionOutcome: ...

    async def perform_download(
        self,
        request: BackendActionRequest,
    ) -> BackendDownloadOutcome: ...

    async def perform_coordinate_commit(
        self,
        request: BackendCoordinateRequest,
        *,
        expected: BackendCoordinatePreflight | None = None,
    ) -> BackendActionOutcome: ...

    async def preflight_coordinate_commit(
        self,
        request: BackendCoordinateRequest,
    ) -> BackendCoordinatePreflight: ...

    async def close(self) -> None: ...


class BrowserSessionHandle(Protocol):
    @property
    def process_owned(self) -> bool: ...

    @property
    def pages_owned(self) -> bool: ...

    @property
    def connected(self) -> bool: ...

    async def pages(self) -> tuple[BrowserPageHandle, ...]: ...

    def take_page_overflow_count(self) -> int: ...

    async def close(self) -> None: ...


class BrowserBackend(Protocol):
    async def open_session(
        self,
        options: BrowserOpenOptions,
        *,
        destination_guard: DestinationGuard,
    ) -> BrowserSessionHandle: ...

    async def aclose(self) -> None: ...


type BrowserBackendFactory = Callable[[], BrowserBackend]
