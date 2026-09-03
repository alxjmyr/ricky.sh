"""Strict browser-control boundary models."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.attachments import BrowserDownloadRef
from ricky.llm import MediaArtifactRef
from ricky.profiles import ProfileResourceRef
from ricky.protected_values import ProtectedControlKind

BrowserActionKind = Literal[
    "click",
    "fill",
    "select",
    "set_checked",
    "press_key",
    "commit",
    "upload",
    "download",
    "coordinate_click",
    "coordinate_commit",
    "protected_fill",
]
BrowserEffectDisposition = Literal["not_performed", "performed", "in_doubt"]
BrowserResourceKind = Literal["persistent", "cdp"]
BrowserResourceAvailability = Literal["available", "busy", "unavailable"]
BrowserSessionMode = Literal[
    "owned_ephemeral",
    "owned_persistent",
    "attached_cdp",
    "attached_selected_tab",
]


class BrowserModel(BaseModel):
    """Base for serialized browser boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BrowserFailure(BrowserModel):
    code: Literal[
        "disabled",
        "not_installed",
        "invalid_destination",
        "destination_blocked",
        "session_limit",
        "page_limit",
        "unknown_resource",
        "resource_kind_mismatch",
        "resource_busy",
        "attachment_unavailable",
        "attachment_timeout",
        "attachment_disconnected",
        "unsupported_attached_page",
        "attached_page_limit",
        "unknown_session",
        "unknown_page",
        "session_closed",
        "page_closed",
        "navigation_timeout",
        "operation_timeout",
        "download_blocked",
        "download_unavailable",
        "download_too_large",
        "download_publish_failed",
        "upload_failed",
        "screenshot_denied",
        "screenshot_too_large",
        "visual_capture_failed",
        "coordinate_out_of_bounds",
        "stale_target",
        "invented_target",
        "ambiguous_target",
        "incompatible_target",
        "consequential_target",
        "transaction_envelope",
        "semantic_target_available",
        "semantic_snapshot_required",
        "unattended_denied",
        "browser_budget_exhausted",
        "protected_field",
        "file_control",
        "handoff_required",
        "unexpected_dialog",
        "action_in_doubt",
        "backend_error",
    ]
    message: str = Field(min_length=1, max_length=2_000)
    retryable: bool = False
    outcome_uncertain: bool = False
    replacement_target: BrowserActionTarget | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _replacement_target_is_coherent(self) -> BrowserFailure:
        if (self.replacement_target is not None) != (self.code == "semantic_target_available"):
            raise ValueError("replacement target requires semantic_target_available")
        return self


_CanonicalMoneyAmount = Annotated[
    str,
    Field(
        min_length=1,
        max_length=25,
        pattern=r"^(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,6})?$",
    ),
]
_EnvelopeText = Annotated[str, Field(min_length=1, max_length=1_000)]
_EnvelopeListText = Annotated[str, Field(min_length=1, max_length=1_000)]
_DestinationProjection = Annotated[str, Field(min_length=1, max_length=4_000)]
_UNMASKED_LONG_NUMBER_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_UNMASKED_ACCOUNT_RE = re.compile(
    r"\b(?:account|acct|routing)\s*(?:number|no\.?|#)?\s*[:=#-]?\s*"
    r"(?:\d[ -]?){5,}\d\b",
    re.IGNORECASE,
)
_UNMASKED_IBAN_RE = re.compile(
    r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9][ ]?){10,30}\b",
    re.IGNORECASE,
)
_UNMASKED_SECURITY_CODE_RE = re.compile(
    r"\b(?:cvv2?|cvc2?|cid|card\s+security\s+code|security\s+code)"
    r"\s*(?:is|:|#)?\s*\d{3,4}\b",
    re.IGNORECASE,
)


def _require_bounded_text(value: str, *, field_name: str) -> str:
    if value != value.strip() or not value:
        raise ValueError(f"{field_name} must be non-blank without surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} cannot contain control characters")
    return value


def _require_bounded_text_items[T: (tuple[str, ...], list[str])](
    values: T,
    *,
    field_name: str,
) -> T:
    for value in values:
        _require_bounded_text(value, field_name=f"{field_name} entry")
    return values


def _contains_unmasked_financial_identifier(value: str) -> bool:
    return any(
        pattern.search(value) is not None
        for pattern in (
            _UNMASKED_LONG_NUMBER_RE,
            _UNMASKED_ACCOUNT_RE,
            _UNMASKED_IBAN_RE,
            _UNMASKED_SECURITY_CODE_RE,
        )
    )


class BrowserMoney(BrowserModel):
    """One exact nonnegative amount in a three-letter currency."""

    amount: _CanonicalMoneyAmount
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @field_validator("amount")
    @classmethod
    def _bounded_precision(cls, value: str) -> str:
        if sum(character.isdigit() for character in value) > 18:
            raise ValueError("money amount precision cannot exceed 18 digits")
        return value


class BrowserFinancialComponent(BrowserModel):
    """One nonnegative line item or fee disclosed in a financial review."""

    label: str = Field(min_length=1, max_length=500)
    amount: BrowserMoney

    @field_validator("label")
    @classmethod
    def _nonblank_label(cls, value: str) -> str:
        return _require_bounded_text(value, field_name="financial component label")


class BrowserRecurrence(BrowserModel):
    """Exact continuing-charge terms for one recurring financial proposal."""

    amount: BrowserMoney
    cadence: str = Field(min_length=1, max_length=200)
    start_date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    end_date: str | None = Field(
        default=None,
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
    )
    open_ended: bool
    cancellation: str = Field(min_length=1, max_length=1_000)

    @field_validator("cadence", "cancellation")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        return _require_bounded_text(value, field_name="recurrence text")

    @field_validator("start_date", "end_date")
    @classmethod
    def _real_calendar_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def _coherent_dates_and_amount(self) -> BrowserRecurrence:
        if self.open_ended == (self.end_date is not None):
            raise ValueError(
                "open-ended recurrence must omit end_date; bounded recurrence requires it"
            )
        if self.end_date is not None and self.end_date <= self.start_date:
            raise ValueError("recurrence end_date must be after start_date")
        if Decimal(self.amount.amount) <= 0:
            raise ValueError("recurring amount must be greater than zero")
        return self


class BrowserProtectedValueFundingSource(BrowserModel):
    """Safe alias for a protected funding source used on the current page."""

    kind: Literal["protected_value"]
    protected_value: ProfileResourceRef


class BrowserSiteFundingSource(BrowserModel):
    """User-facing label for a source already stored by the website."""

    kind: Literal["site"]
    label: str = Field(min_length=1, max_length=200)

    @field_validator("label")
    @classmethod
    def _nonblank_label(cls, value: str) -> str:
        value = _require_bounded_text(value, field_name="site funding source label")
        if _contains_unmasked_financial_identifier(value):
            raise ValueError(
                "site funding source labels cannot contain unmasked financial identifiers"
            )
        return value


BrowserFundingSource = Annotated[
    BrowserProtectedValueFundingSource | BrowserSiteFundingSource,
    Field(discriminator="kind"),
]


class BrowserTransactionEnvelope(BrowserModel):
    """Proposed non-financial consequential browser commit."""

    kind: Literal["browser"]
    intent: _EnvelopeText
    destination: str = Field(min_length=1, max_length=500)
    consequences: tuple[_EnvelopeListText, ...] = Field(min_length=1, max_length=20)
    disclosures: tuple[_EnvelopeListText, ...] = Field(max_length=20)
    expected_result: _EnvelopeText

    @field_validator("intent", "destination", "expected_result")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        return _require_bounded_text(value, field_name="browser transaction text")

    @field_validator("consequences", "disclosures")
    @classmethod
    def _nonblank_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_bounded_text_items(values, field_name="browser transaction list")


class BrowserFinancialTransactionEnvelope(BrowserModel):
    """Proposed payment, money movement, or monetary obligation."""

    kind: Literal["financial"]
    intent: _EnvelopeText
    payee: str = Field(min_length=1, max_length=500)
    total: BrowserMoney
    components: tuple[BrowserFinancialComponent, ...] = Field(default=(), max_length=50)
    fees: tuple[BrowserFinancialComponent, ...] = Field(max_length=20)
    timing: Literal["one_time", "recurring"]
    recurrence: BrowserRecurrence | None = None
    source: BrowserFundingSource
    consequences: tuple[_EnvelopeListText, ...] = Field(min_length=1, max_length=20)
    expected_result: _EnvelopeText

    @field_validator("intent", "payee", "expected_result")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        return _require_bounded_text(value, field_name="financial transaction text")

    @field_validator("consequences")
    @classmethod
    def _nonblank_consequences(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_bounded_text_items(values, field_name="financial consequences")

    @model_validator(mode="after")
    def _coherent_financial_terms(self) -> BrowserFinancialTransactionEnvelope:
        if (self.timing == "recurring") != (self.recurrence is not None):
            raise ValueError("recurring timing requires recurrence and one_time forbids it")
        currencies = {component.amount.currency for component in (*self.components, *self.fees)}
        if self.recurrence is not None:
            currencies.add(self.recurrence.amount.currency)
        if currencies - {self.total.currency}:
            raise ValueError("all financial amounts must use the total currency")
        return self


BrowserCommitEnvelope = Annotated[
    BrowserTransactionEnvelope | BrowserFinancialTransactionEnvelope,
    Field(discriminator="kind"),
]


class BrowserTransactionEvidence(BrowserModel):
    """Compact transaction reference safe for results and later page reads."""

    envelope_kind: Literal["browser", "financial"]
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    top_level_origin: str = Field(min_length=1, max_length=500)
    target_frame_origin: str = Field(min_length=1, max_length=500)
    effective_destinations: tuple[_DestinationProjection, ...] = Field(
        default=(),
        max_length=20,
    )

    @field_validator("top_level_origin", "target_frame_origin")
    @classmethod
    def _nonblank_origins(cls, value: str) -> str:
        return _require_bounded_text(value, field_name="transaction origin")

    @field_validator("effective_destinations", mode="before")
    @classmethod
    def _normalize_destinations(cls, values: object) -> object:
        if not isinstance(values, (list, tuple)):
            raise ValueError("effective destinations must be a list or tuple")
        return tuple(values)

    @field_validator("effective_destinations")
    @classmethod
    def _bounded_destinations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_bounded_text_items(
            values,
            field_name="effective destination projections",
        )


class BrowserActionEvidence(BrowserModel):
    """Compact latest-action evidence safe to surface on later page reads."""

    action_id: str = Field(pattern=r"^browser_action_[0-9a-f]{32}$")
    kind: BrowserActionKind
    disposition: BrowserEffectDisposition
    failure: BrowserFailure | None = None
    protected_ref: ProfileResourceRef | None = None
    protected_field: str | None = Field(default=None, max_length=64)
    transaction: BrowserTransactionEvidence | None = None

    @model_validator(mode="after")
    def _protected_evidence_is_coherent(self) -> BrowserActionEvidence:
        present = self.protected_ref is not None or self.protected_field is not None
        if present != (self.kind == "protected_fill"):
            raise ValueError("protected browser evidence requires protected_fill")
        if self.kind == "protected_fill" and (
            self.protected_ref is None or self.protected_field is None
        ):
            raise ValueError("protected fill evidence requires an alias and field")
        transaction_action = self.kind in {"commit", "coordinate_commit"}
        if transaction_action != (self.transaction is not None):
            raise ValueError("commit actions require transaction evidence")
        return self


class BrowserPage(BrowserModel):
    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    page_id: str = Field(pattern=r"^browser_page_[0-9a-f]{32}$")
    selected: bool
    url: str = Field(min_length=1, max_length=4_000)
    origin: str | None = Field(default=None, max_length=500)
    title: str = Field(default="", max_length=1_000)
    navigation_generation: int = Field(ge=0)
    latest_action: BrowserActionEvidence | None = None


class BrowserResource(BrowserModel):
    """Provider-safe metadata for one configured profile-owned browser resource."""

    resource: ProfileResourceRef
    kind: BrowserResourceKind
    description: str = Field(min_length=1, max_length=500)
    availability: BrowserResourceAvailability
    headless: bool | None = None
    process_owned: bool


class BrowserResourceList(BrowserModel):
    resources: tuple[BrowserResource, ...] = Field(default=(), max_length=100)


class BrowserResourceReset(BrowserModel):
    resource: ProfileResourceRef
    reset: bool = True


class BrowserSession(BrowserModel):
    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    resource: ProfileResourceRef
    mode: BrowserSessionMode = "owned_ephemeral"
    headless: bool | None
    process_owned: bool = True
    selected_page_id: str = Field(pattern=r"^browser_page_[0-9a-f]{32}$")
    pages: tuple[BrowserPage, ...] = Field(min_length=1)


class BrowserSessionClosed(BrowserModel):
    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    closed: bool = True


class BrowserPageList(BrowserModel):
    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    selected_page_id: str = Field(pattern=r"^browser_page_[0-9a-f]{32}$")
    pages: tuple[BrowserPage, ...]


class BrowserNavigation(BrowserModel):
    page: BrowserPage


class BrowserScroll(BrowserModel):
    page: BrowserPage
    direction: Literal["up", "down"]
    amount: int = Field(ge=1, le=5_000)


class BrowserTarget(BrowserModel):
    ref: str = Field(pattern=r"^(?:(?:f[0-9]+)?e[0-9]+|d[0-9]+)$", max_length=100)
    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    page_id: str = Field(pattern=r"^browser_page_[0-9a-f]{32}$")
    navigation_generation: int = Field(ge=0)
    snapshot_id: str = Field(pattern=r"^browser_snapshot_[0-9a-f]{32}$")


BrowserControlKind = Literal[
    "link",
    "button",
    "text",
    "search",
    "email",
    "telephone",
    "url",
    "date",
    "number",
    "textarea",
    "select",
    "checkbox",
    "radio",
    "contenteditable",
    "file",
    "other",
]


class BrowserTargetDescriptor(BrowserModel):
    """Provider-safe facts for choosing a specialized element action."""

    ref: str = Field(pattern=r"^(?:(?:f[0-9]+)?e[0-9]+|d[0-9]+)$", max_length=100)
    role: str = Field(default="", max_length=100)
    name: str = Field(default="", max_length=500)
    control_kind: BrowserControlKind = "other"
    frame_origin: str | None = Field(default=None, max_length=500)
    checked: bool | None = None
    disabled: bool = False
    editable: bool = False
    option_labels: tuple[str, ...] = Field(default=(), max_length=200)
    consequential: bool = False
    protected: bool = False
    protected_kind: ProtectedControlKind | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    file: bool = False
    multiple: bool = False
    accept: tuple[str, ...] = Field(default=(), max_length=100)


class BrowserActionTarget(BrowserModel):
    """Model-supplied opaque reference to one cached snapshot target."""

    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    page_id: str = Field(pattern=r"^browser_page_[0-9a-f]{32}$")
    snapshot_id: str = Field(pattern=r"^browser_snapshot_[0-9a-f]{32}$")
    ref: str = Field(pattern=r"^(?:(?:f[0-9]+)?e[0-9]+|d[0-9]+)$", max_length=100)


CoordinateFallbackReason = Literal[
    "no_supported_semantic_target",
    "custom_rendered_target",
    "semantic_preflight_not_dispatched",
]


class CoordinateFallbackEvidence(BrowserModel):
    """Harness-issued proof that one coordinate click is a last resort."""

    semantic_snapshot_id: str = Field(pattern=r"^browser_snapshot_[0-9a-f]{32}$")
    reason: CoordinateFallbackReason
    failed_semantic_target: BrowserActionTarget | None = None
    semantic_failure_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{0,99}$",
    )

    @model_validator(mode="after")
    def _failed_preflight_is_coherent(self) -> CoordinateFallbackEvidence:
        failed = self.reason == "semantic_preflight_not_dispatched"
        if failed != (
            self.failed_semantic_target is not None and self.semantic_failure_code is not None
        ):
            raise ValueError(
                "semantic preflight fallback requires its target and bounded failure code"
            )
        return self


class BrowserAttachmentUseBinding(BrowserModel):
    """Safe exact uploaded-file evidence retained on one page generation."""

    id: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0, le=10_000_000_000)


class BrowserProtectedUseBinding(BrowserModel):
    """Safe exact protected-value evidence retained on one page generation."""

    resource: ProfileResourceRef
    revision: int = Field(ge=1)
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


BrowserKey = Literal[
    "Tab",
    "Escape",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Backspace",
    "Delete",
]
BrowserCommitActivation = Literal["click", "enter", "space"]
BrowserDialogResponse = Literal["dismiss", "accept"]


class BrowserDialogPolicy(BrowserModel):
    """Dialog response installed before a single browser action is dispatched."""

    response: BrowserDialogResponse = "dismiss"
    prompt_text: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def _prompt_text_requires_accept(self) -> BrowserDialogPolicy:
        if self.prompt_text is not None and self.response != "accept":
            raise ValueError("prompt_text requires an accepting dialog policy")
        return self


class BrowserActionRequest(BrowserModel):
    """One specialized action with all dialog handling known before dispatch."""

    kind: BrowserActionKind
    value: str | None = Field(default=None, max_length=20_000)
    option_label: str | None = Field(default=None, min_length=1, max_length=1_000)
    checked: bool | None = None
    key: BrowserKey | None = None
    activation: BrowserCommitActivation | None = None
    dialog: BrowserDialogPolicy = Field(default_factory=BrowserDialogPolicy)

    @model_validator(mode="after")
    def _validate_action_payload(self) -> BrowserActionRequest:
        if self.kind == "protected_fill":
            raise ValueError("protected fills require the dedicated in-process request")
        supplied = {
            "value": self.value is not None,
            "option_label": self.option_label is not None,
            "checked": self.checked is not None,
            "key": self.key is not None,
            "activation": self.activation is not None,
        }
        expected: dict[BrowserActionKind, str | None] = {
            "click": None,
            "fill": "value",
            "select": "option_label",
            "set_checked": "checked",
            "press_key": "key",
            "commit": "activation",
            "upload": None,
            "download": None,
            "coordinate_click": None,
            "coordinate_commit": None,
        }
        required = expected[self.kind]
        if required is not None and not supplied[required]:
            raise ValueError(f"{self.kind} requires {required}")
        extras = [name for name, present in supplied.items() if present and name != required]
        if extras:
            raise ValueError(f"{self.kind} does not accept {', '.join(extras)}")
        if self.kind not in {"commit", "coordinate_commit"} and self.dialog != (
            BrowserDialogPolicy()
        ):
            raise ValueError(
                "only commit actions and coordinate commits may override the default dialog policy"
            )
        return self


class BrowserDialogObservation(BrowserModel):
    """Bounded untrusted dialog facts; prompt defaults are deliberately absent."""

    kind: Literal["alert", "confirm", "prompt", "beforeunload"]
    message: str = Field(default="", max_length=2_000)
    response: Literal["dismissed", "accepted", "unhandled"]
    matched_policy: bool


class BrowserPageChanges(BrowserModel):
    created_page_ids: tuple[str, ...] = Field(default=(), max_length=50)
    closed_page_ids: tuple[str, ...] = Field(default=(), max_length=50)
    selected_popup_page_id: str | None = Field(
        default=None,
        pattern=r"^browser_page_[0-9a-f]{32}$",
    )


class BrowserPostcondition(BrowserModel):
    navigation_occurred: bool = False
    page_closed: bool = False
    page_changes: BrowserPageChanges = Field(default_factory=BrowserPageChanges)
    observation_limited: bool = False
    observation_note: str | None = Field(default=None, max_length=1_000)


class BrowserActionContext(BrowserModel):
    """Trusted cached facts used synchronously for permission and identity."""

    target: BrowserActionTarget
    resource: ProfileResourceRef
    navigation_generation: int = Field(ge=0)
    url: str = Field(min_length=1, max_length=4_000)
    origin: str | None = Field(default=None, max_length=500)
    descriptor: BrowserTargetDescriptor
    headless: bool | None


class BrowserSnapshot(BrowserModel):
    snapshot_id: str = Field(pattern=r"^browser_snapshot_[0-9a-f]{32}$")
    page: BrowserPage
    content: str = Field(max_length=200_000)
    targets: tuple[BrowserTarget, ...]
    descriptors: tuple[BrowserTargetDescriptor, ...] = ()
    depth_limit: int = Field(ge=1)
    character_limit: int = Field(ge=1)
    character_truncated: bool


class BrowserBoundingBox(BrowserModel):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class BrowserViewport(BrowserModel):
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    scroll_x: float
    scroll_y: float
    device_scale_factor: float = Field(gt=0)
    image_scale: float = Field(gt=0, le=1)


class BrowserVisualCandidate(BrowserModel):
    number: int = Field(ge=1)
    target: BrowserTarget
    descriptor: BrowserTargetDescriptor
    bounding_box: BrowserBoundingBox


class BrowserVisualSnapshot(BrowserModel):
    snapshot_id: str = Field(pattern=r"^browser_snapshot_[0-9a-f]{32}$")
    page: BrowserPage
    image: MediaArtifactRef
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    viewport: BrowserViewport
    candidates: tuple[BrowserVisualCandidate, ...]
    candidate_truncated: bool = False
    masked_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BrowserCoordinateTarget(BrowserModel):
    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    page_id: str = Field(pattern=r"^browser_page_[0-9a-f]{32}$")
    screenshot_id: str = Field(pattern=r"^browser_snapshot_[0-9a-f]{32}$")
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)


class BrowserCoordinateContext(BrowserModel):
    target: BrowserCoordinateTarget
    resource: ProfileResourceRef
    navigation_generation: int = Field(ge=0)
    url: str = Field(min_length=1, max_length=4_000)
    origin: str | None = Field(default=None, max_length=500)
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    css_x: float = Field(ge=0)
    css_y: float = Field(ge=0)
    masked_base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BrowserDownloadResult(BrowserModel):
    action_id: str = Field(pattern=r"^browser_action_[0-9a-f]{32}$")
    disposition: BrowserEffectDisposition
    page: BrowserPage
    download: BrowserDownloadRef | None = None
    failure: BrowserFailure | None = None


class BrowserActionResult(BrowserModel):
    """Conservative browser-level evidence for exactly one action attempt."""

    action_id: str = Field(pattern=r"^browser_action_[0-9a-f]{32}$")
    kind: BrowserActionKind
    disposition: BrowserEffectDisposition
    page: BrowserPage
    snapshot: BrowserSnapshot | None = None
    dialogs: tuple[BrowserDialogObservation, ...] = Field(default=(), max_length=20)
    postcondition: BrowserPostcondition = Field(default_factory=BrowserPostcondition)
    failure: BrowserFailure | None = None
    transaction: BrowserTransactionEvidence | None = None

    @model_validator(mode="after")
    def _transaction_evidence_is_coherent(self) -> BrowserActionResult:
        transaction_action = self.kind in {"commit", "coordinate_commit"}
        if transaction_action != (self.transaction is not None):
            raise ValueError("commit actions require transaction evidence")
        return self


BrowserHandoffReason = Literal[
    "captcha",
    "passkey",
    "sso",
    "protected_field",
    "ambiguous_interface",
]


class BrowserHandoff(BrowserModel):
    """Trusted request for one fixed class of headed local user interaction."""

    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    page_id: str = Field(pattern=r"^browser_page_[0-9a-f]{32}$")
    reason: BrowserHandoffReason
    prompt: str = Field(min_length=1, max_length=1_000)


class BrowserInstallStatus(BrowserModel):
    enabled: bool
    browser: Literal["chromium"] = "chromium"
    ready: bool
    install_dir: str = Field(min_length=1, max_length=4_000)
    executable: str | None = Field(default=None, max_length=4_000)
    repair_command: str = "uv run ricky browser install"


class BrowserError(RuntimeError):
    """Bounded browser error safe to expose through a tool result."""

    def __init__(self, failure: BrowserFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure
