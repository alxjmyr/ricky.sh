"""Strict contracts for bounded background browser execution and approval."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from ricky.browser.types import BrowserCommitEnvelope
from ricky.profiles import ProfileLabel, ProfileName, ProfileResourceRef

BrowserExecutionMode = Literal["read_only", "transaction"]
BrowserResourceKind = Literal["ephemeral", "persistent"]
BrowserAttemptStatus = Literal[
    "starting",
    "running",
    "parked",
    "completed",
    "failed",
    "cancelled",
    "uncertain",
    "in_doubt",
]
BrowserCleanupDisposition = Literal["pending", "not_started", "confirmed", "failed"]
BrowserTransactionState = Literal[
    "pending",
    "approved",
    "denied",
    "expired",
    "invalidated",
    "consumed",
]
BrowserApprovalKind = Literal["browser_transaction", "protected_destination"]
BrowserCommitTargetMode = Literal["semantic", "coordinate"]
BrowserBudgetOperation = Literal[
    "session_starts",
    "navigations",
    "scrolls",
    "created_pages",
    "controlled_pages",
    "semantic_observations",
    "visual_observations",
    "interactions",
    "protected_materializations",
    "uploads",
    "upload_bytes",
    "downloads",
    "download_bytes",
    "transaction_commits",
    "parked_browsers",
]
BrowserEvidenceDisposition = Literal[
    "observed",
    "reserved",
    "performed",
    "not_performed",
    "in_doubt",
]

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT_RE = re.compile(r"^browser_attempt_[0-9a-f]{32}$")
_TRANSACTION_RE = re.compile(r"^browser_transaction_[0-9a-f]{32}$")
_DESTINATION_APPROVAL_RE = re.compile(r"^browser_destination_[0-9a-f]{32}$")
_LOGICAL_RE = re.compile(r"^browser_logical_[0-9a-f]{32}$")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BrowserExecutionBudget(_FrozenModel):
    """Cumulative and live-resource ceilings for one browser execution."""

    session_starts: int = Field(ge=0, le=100)
    navigations: int = Field(ge=0, le=10_000)
    scrolls: int = Field(ge=0, le=10_000)
    created_pages: int = Field(ge=0, le=1_000)
    controlled_pages: int = Field(ge=0, le=100)
    semantic_observations: int = Field(ge=0, le=10_000)
    visual_observations: int = Field(ge=0, le=1_000)
    interactions: int = Field(ge=0, le=10_000)
    protected_materializations: int = Field(ge=0, le=1_000)
    uploads: int = Field(ge=0, le=1_000)
    upload_bytes: int = Field(ge=0, le=10_000_000_000)
    downloads: int = Field(ge=0, le=1_000)
    download_bytes: int = Field(ge=0, le=10_000_000_000)
    transaction_commits: int = Field(ge=0, le=100)
    parked_browsers: int = Field(ge=0, le=10)
    approval_ttl_seconds: int = Field(ge=30, le=86_400)

    def ceiling(self, operation: BrowserBudgetOperation) -> int:
        return int(getattr(self, operation))


class BrowserResourcePin(_FrozenModel):
    """One exact configured browser revision admitted by a contract."""

    resource: ProfileResourceRef
    kind: BrowserResourceKind
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authenticated_origin_ceiling: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator("authenticated_origin_ceiling")
    @classmethod
    def _origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_origins(values)


class BrowserAttachmentPin(_FrozenModel):
    """Safe immutable identity for one execution-authorized upload source."""

    id: str = Field(min_length=1, max_length=500)
    profile: ProfileName
    task_id: str = Field(pattern=r"^task_[0-9a-f]{32}$")
    artifact_path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=0, le=10_000_000_000)

    @model_validator(mode="after")
    def _source(self) -> BrowserAttachmentPin:
        path = PurePosixPath(self.artifact_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(part in {"", "."} or part.startswith(".ricky-task") for part in path.parts)
        ):
            raise ValueError("browser attachment artifact path is unsafe")
        expected = f"task/{self.profile}/{self.task_id}/{self.artifact_path}"
        if self.id != expected:
            raise ValueError("browser attachment id differs from its exact task artifact source")
        return self


class BrowserProtectedResourcePin(_FrozenModel):
    """Non-secret protected-resource ceiling compiled into an execution."""

    resource: ProfileResourceRef
    revision: int = Field(ge=1)
    fields: tuple[str, ...] = Field(min_length=1, max_length=100)
    materialization_limit: int = Field(ge=0, le=1_000)
    commit_limit: int = Field(ge=0, le=100)

    @field_validator("fields")
    @classmethod
    def _fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_plain_items(values, name="protected fields")


class BrowserExecutionScope(_FrozenModel):
    """Immutable browser authority and budget ceiling for one background run."""

    version: Literal[1] = 1
    mode: BrowserExecutionMode
    resources: tuple[BrowserResourcePin, ...] = Field(default=(), max_length=50)
    allow_ephemeral: bool = False
    allow_public_https_research: bool = False
    https_only_transactions: bool = True
    private_origin_ceiling: tuple[str, ...] = Field(default=(), max_length=100)
    allowed_tools: tuple[str, ...] = Field(min_length=1, max_length=100)
    allowed_operations: tuple[BrowserBudgetOperation, ...] = Field(min_length=1, max_length=15)
    allow_masked_visual_observations: bool = False
    attachments: tuple[BrowserAttachmentPin, ...] = Field(default=(), max_length=100)
    protected_resources: tuple[BrowserProtectedResourcePin, ...] = Field(default=(), max_length=100)
    budget: BrowserExecutionBudget

    @field_validator("private_origin_ceiling")
    @classmethod
    def _private_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_origins(values)

    @field_validator("allowed_tools")
    @classmethod
    def _tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_plain_items(values, name="browser tools", pattern=r"^[a-z][a-z0-9_]*$")

    @field_validator("allowed_operations")
    @classmethod
    def _operations(
        cls, values: tuple[BrowserBudgetOperation, ...]
    ) -> tuple[BrowserBudgetOperation, ...]:
        if len(values) != len(set(values)):
            raise ValueError("browser operations must be unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def _coherent_scope(self) -> BrowserExecutionScope:
        refs = [item.resource.qualified for item in self.resources]
        if len(refs) != len(set(refs)):
            raise ValueError("browser resources must be unique")
        attachments = [item.id for item in self.attachments]
        if len(attachments) != len(set(attachments)):
            raise ValueError("browser attachments must be unique")
        protected = [item.resource.qualified for item in self.protected_resources]
        if len(protected) != len(set(protected)):
            raise ValueError("protected browser resources must be unique")
        ephemeral_launch = self.allow_ephemeral and not self.resources
        persistent_launch = (
            not self.allow_ephemeral
            and len(self.resources) == 1
            and self.resources[0].kind == "persistent"
        )
        if ephemeral_launch == persistent_launch:
            raise ValueError(
                "browser scope requires exactly one ephemeral or persistent launch choice"
            )
        if self.budget.controlled_pages < 1:
            raise ValueError("browser scope must admit its initial controlled page")
        operations = set(self.allowed_operations)
        if self.mode == "read_only":
            forbidden = {
                "interactions",
                "protected_materializations",
                "uploads",
                "upload_bytes",
                "downloads",
                "download_bytes",
                "transaction_commits",
                "parked_browsers",
            }
            if operations & forbidden:
                raise ValueError("read-only browser scopes cannot authorize mutations")
            if self.protected_resources or self.attachments:
                raise ValueError("read-only browser scopes cannot carry protected or upload pins")
        elif not self.https_only_transactions:
            raise ValueError("background transaction scopes must enforce HTTPS")
        if not self.allow_masked_visual_observations and "visual_observations" in operations:
            raise ValueError("visual operation requires explicit masked-image disclosure")
        return self

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class BrowserAttempt(_FrozenModel):
    """Durable lifecycle evidence for one live browser owned by a job run."""

    id: str
    run_id: str = Field(min_length=1, max_length=100)
    execution_request_id: str | None = Field(default=None, pattern=r"^execution_[0-9a-f]{32}$")
    profile_label: ProfileLabel
    claim_fence: int = Field(ge=1)
    worker_id: str = Field(min_length=1, max_length=200)
    mode: BrowserExecutionMode
    resource: ProfileResourceRef | None = None
    resource_kind: BrowserResourceKind
    resource_configuration_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: BrowserAttemptStatus
    cleanup: BrowserCleanupDisposition
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    error: str | None = Field(default=None, max_length=2_000)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _ATTEMPT_RE.fullmatch(value) is None:
            raise ValueError("invalid browser attempt id")
        return value

    @model_validator(mode="after")
    def _attempt(self) -> BrowserAttempt:
        for name in ("started_at", "updated_at", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                _utc(value, name)
        if self.updated_at < self.started_at:
            raise ValueError("browser attempt update precedes start")
        terminal = self.status in {"completed", "failed", "cancelled", "uncertain", "in_doubt"}
        if terminal != (self.finished_at is not None):
            raise ValueError("only terminal browser attempts carry finished_at")
        if self.resource_kind == "persistent" and self.resource is None:
            raise ValueError("persistent browser attempts require an exact resource")
        if self.resource is None and self.resource_configuration_digest is not None:
            raise ValueError("ephemeral browser attempts cannot carry a resource digest")
        return self


class BrowserAttemptLease(_FrozenModel):
    """Private in-process mutation token for one durable browser attempt."""

    attempt: BrowserAttempt
    owner_token: str = Field(pattern=r"^browser_owner_[0-9a-f]{32}$")


class BrowserBudgetUsage(_FrozenModel):
    attempt_id: str
    operation: BrowserBudgetOperation
    used: int = Field(ge=0)
    ceiling: int = Field(ge=0)
    updated_at: datetime

    @model_validator(mode="after")
    def _usage(self) -> BrowserBudgetUsage:
        if self.used > self.ceiling:
            raise ValueError("browser budget usage exceeds its ceiling")
        _utc(self.updated_at, "updated_at")
        return self


class BrowserBudgetReservation(_FrozenModel):
    """Durable maximum-cost preauthorization settled by definitive evidence."""

    id: str = Field(pattern=r"^browser_budget_[0-9a-f]{32}$")
    attempt_id: str
    operation: Literal["created_pages"]
    reservation_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    reserved: int = Field(ge=1)
    consumed: int = Field(ge=0)
    state: Literal["pending", "settled", "in_doubt"]
    created_at: datetime
    settled_at: datetime | None = None

    @model_validator(mode="after")
    def _reservation(self) -> BrowserBudgetReservation:
        _utc(self.created_at, "created_at")
        if self.settled_at is not None:
            _utc(self.settled_at, "settled_at")
        if self.consumed > self.reserved:
            raise ValueError("browser budget settlement exceeds its reservation")
        if (self.state == "pending") == (self.settled_at is not None):
            raise ValueError("only settled browser budget reservations carry settled_at")
        return self


class BrowserNavigationCheckpoint(_FrozenModel):
    id: int | None = Field(default=None, ge=1)
    attempt_id: str
    page_generation: int = Field(ge=0)
    top_level_origin: str = Field(min_length=1, max_length=500)
    url_projection: str = Field(min_length=1, max_length=2_000)
    created_at: datetime

    @field_validator("top_level_origin")
    @classmethod
    def _origin(cls, value: str) -> str:
        return _canonical_origin(value)

    @field_validator("url_projection")
    @classmethod
    def _projection(cls, value: str) -> str:
        return _plain(value, "URL projection", 2_000)


class BrowserLiveBinding(_FrozenModel):
    """Safe digest-bound projection of one live browser occurrence."""

    occurrence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource: ProfileResourceRef | None = None
    resource_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resource_kind: BrowserResourceKind | None = None
    resource_configuration_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    provider: str | None = Field(default=None, min_length=1, max_length=100)
    session_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    page_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    budget_ceiling: BrowserExecutionBudget | None = None
    page_generation: int = Field(ge=0)
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_description: str = Field(min_length=1, max_length=1_000)
    top_level_origin: str = Field(min_length=1, max_length=500)
    target_frame_origin: str = Field(min_length=1, max_length=500)
    destination_projections: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("target_description")
    @classmethod
    def _target(cls, value: str) -> str:
        return _plain(value, "target description", 1_000)

    @field_validator("top_level_origin", "target_frame_origin")
    @classmethod
    def _origin(cls, value: str) -> str:
        return _canonical_origin(value)

    @field_validator("destination_projections")
    @classmethod
    def _destinations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_plain_items(values, name="destination projections", max_length=2_000)

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str | None) -> str | None:
        return None if value is None else _plain(value, "browser provider", 100)

    def has_transaction_review_context(self) -> bool:
        """Return whether this binding carries the complete durable review context."""

        required = (
            self.resource_digest,
            self.resource_kind,
            self.provider,
            self.session_digest,
            self.page_digest,
            self.budget_ceiling,
        )
        if any(value is None for value in required):
            return False
        if self.resource_kind == "persistent":
            return self.resource is not None and self.resource_configuration_digest is not None
        return self.resource is None and self.resource_configuration_digest is None


class CoordinateFallbackBinding(_FrozenModel):
    """Durable non-pixel evidence binding one last-resort coordinate approval."""

    reason: Literal[
        "no_semantic_target",
        "position_sensitive_surface",
        "semantic_preflight_not_performed",
    ]
    semantic_resolution_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    equivalent_semantic_target: str | None = Field(default=None, max_length=500)
    masked_image_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    visual_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    x: Annotated[float, Field(ge=0, le=100_000, allow_inf_nan=False)]
    y: Annotated[float, Field(ge=0, le=100_000, allow_inf_nan=False)]
    viewport_width: int = Field(ge=1, le=100_000)
    viewport_height: int = Field(ge=1, le=100_000)
    scroll_x: Annotated[
        float,
        Field(ge=-10_000_000, le=10_000_000, allow_inf_nan=False),
    ]
    scroll_y: Annotated[
        float,
        Field(ge=-10_000_000, le=10_000_000, allow_inf_nan=False),
    ]
    coordinate_scale: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)]
    nested_hit_target_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _fallback(self) -> CoordinateFallbackBinding:
        if self.equivalent_semantic_target is not None:
            raise ValueError("coordinate fallback is forbidden when a semantic target exists")
        if self.x >= self.viewport_width or self.y >= self.viewport_height:
            raise ValueError("coordinate lies outside the approved viewport")
        return self


class BrowserProtectedUseEvidence(_FrozenModel):
    resource: ProfileResourceRef
    revision: int = Field(ge=1)
    field: str = Field(min_length=1, max_length=200)

    @field_validator("field")
    @classmethod
    def _field(cls, value: str) -> str:
        return _plain(value, "protected field", 200)


class BrowserActionEvidence(_FrozenModel):
    """Bounded browser evidence linked to the shared durable effect ledger."""

    id: int | None = Field(default=None, ge=1)
    attempt_id: str
    action_id: str | None = Field(default=None, max_length=100)
    operation: str = Field(min_length=1, max_length=200)
    logical_effect_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    live_occurrence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: BrowserEvidenceDisposition
    attempt_reason: str | None = Field(default=None, max_length=200)
    binding: BrowserLiveBinding | None = None
    postcondition: str | None = Field(default=None, max_length=2_000)
    created_at: datetime

    @model_validator(mode="after")
    def _evidence(self) -> BrowserActionEvidence:
        _utc(self.created_at, "created_at")
        if self.disposition in {"reserved", "performed", "not_performed", "in_doubt"} and (
            self.logical_effect_key is None
        ):
            raise ValueError("effectful browser evidence requires a logical effect key")
        return self


class _BrowserApprovalDraft(_FrozenModel):
    """Exact live approval facts before the store issues a one-time challenge."""

    version: Literal[1] = 1
    request_id: str = Field(pattern=r"^execution_[0-9a-f]{32}$")
    run_id: str = Field(min_length=1, max_length=100)
    attempt_id: str
    claim_fence: int = Field(ge=1)
    prepared_effect_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_effect_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding: BrowserLiveBinding
    principal_id: str = Field(min_length=1, max_length=500)
    conversation_id: str = Field(min_length=1, max_length=512)
    proposal_source_message_id: str = Field(min_length=1, max_length=512)
    created_at: datetime
    expires_at: datetime

    @field_validator("attempt_id")
    @classmethod
    def _attempt_id(cls, value: str) -> str:
        if _ATTEMPT_RE.fullmatch(value) is None:
            raise ValueError("invalid browser attempt id")
        return value

    @model_validator(mode="after")
    def _times(self) -> _BrowserApprovalDraft:
        _utc(self.created_at, "created_at")
        _utc(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("approval expiry must follow creation")
        return self


class BrowserTransactionApprovalDraft(_BrowserApprovalDraft):
    kind: Literal["browser_transaction"] = "browser_transaction"
    id: str
    logical_transaction_id: str
    target_mode: BrowserCommitTargetMode
    envelope: BrowserCommitEnvelope
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    coordinate: CoordinateFallbackBinding | None = None
    attachments: tuple[BrowserAttachmentPin, ...] = Field(default=(), max_length=100)
    protected_uses: tuple[BrowserProtectedUseEvidence, ...] = Field(default=(), max_length=100)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _TRANSACTION_RE.fullmatch(value) is None:
            raise ValueError("invalid parked browser transaction id")
        return value

    @field_validator("logical_transaction_id")
    @classmethod
    def _logical_id(cls, value: str) -> str:
        if _LOGICAL_RE.fullmatch(value) is None:
            raise ValueError("invalid logical browser transaction id")
        return value

    @model_validator(mode="after")
    def _transaction(self) -> BrowserTransactionApprovalDraft:
        if _digest(self.envelope.model_dump(mode="json")) != self.envelope_digest:
            raise ValueError("browser transaction envelope digest mismatch")
        if (self.target_mode == "coordinate") != (self.coordinate is not None):
            raise ValueError("coordinate commits require coordinate fallback binding")
        if not self.binding.has_transaction_review_context():
            raise ValueError("browser transaction approval lacks trusted review context")
        return self


class ProtectedDestinationApprovalDraft(_BrowserApprovalDraft):
    kind: Literal["protected_destination"] = "protected_destination"
    id: str
    protected_use: BrowserProtectedUseEvidence

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _DESTINATION_APPROVAL_RE.fullmatch(value) is None:
            raise ValueError("invalid protected destination approval id")
        return value


BrowserApprovalDraft = Annotated[
    BrowserTransactionApprovalDraft | ProtectedDestinationApprovalDraft,
    Field(discriminator="kind"),
]


class _ParkedBrowserApproval(_FrozenModel):
    """Source-bound durable state shared by exact live approval occurrences."""

    version: Literal[1] = 1
    id: str
    request_id: str = Field(pattern=r"^execution_[0-9a-f]{32}$")
    run_id: str = Field(min_length=1, max_length=100)
    attempt_id: str
    claim_fence: int = Field(ge=1)
    prepared_effect_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_effect_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding: BrowserLiveBinding
    principal_id: str = Field(min_length=1, max_length=500)
    conversation_id: str = Field(min_length=1, max_length=512)
    proposal_source_message_id: str = Field(min_length=1, max_length=512)
    challenge_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: BrowserTransactionState
    revision: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decision_principal_id: str | None = Field(default=None, max_length=500)
    decision_source_message_id: str | None = Field(default=None, max_length=512)
    invalidation_reason: str | None = Field(default=None, max_length=2_000)

    @field_validator("attempt_id")
    @classmethod
    def _attempt_id(cls, value: str) -> str:
        if _ATTEMPT_RE.fullmatch(value) is None:
            raise ValueError("invalid browser attempt id")
        return value

    @model_validator(mode="after")
    def _approval(self) -> _ParkedBrowserApproval:
        for name in ("created_at", "expires_at", "decided_at"):
            value = getattr(self, name)
            if value is not None:
                _utc(value, name)
        if self.expires_at <= self.created_at:
            raise ValueError("transaction approval expiry must follow creation")
        decided = self.state != "pending"
        if decided != (self.decided_at is not None):
            raise ValueError("resolved transaction state requires decided_at")
        if self.state in {"approved", "denied", "consumed"}:
            if self.decision_principal_id is None or self.decision_source_message_id is None:
                raise ValueError("user-resolved approval requires source-bound decision evidence")
        elif self.state == "expired":
            if (self.decision_principal_id is None) != (self.decision_source_message_id is None):
                raise ValueError("expired approval decision evidence must be complete or absent")
        elif self.decision_principal_id is not None or self.decision_source_message_id is not None:
            raise ValueError("only user decisions retain principal and source evidence")
        if self.state == "invalidated" and self.invalidation_reason is None:
            raise ValueError("invalidated approval requires a reason")
        return self


class ParkedBrowserTransaction(_ParkedBrowserApproval):
    """Durable approval record for one exact in-memory prepared commit."""

    kind: Literal["browser_transaction"] = "browser_transaction"
    logical_transaction_id: str
    target_mode: BrowserCommitTargetMode
    envelope: BrowserCommitEnvelope
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    coordinate: CoordinateFallbackBinding | None = None
    attachments: tuple[BrowserAttachmentPin, ...] = Field(default=(), max_length=100)
    protected_uses: tuple[BrowserProtectedUseEvidence, ...] = Field(default=(), max_length=100)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _TRANSACTION_RE.fullmatch(value) is None:
            raise ValueError("invalid parked browser transaction id")
        return value

    @field_validator("logical_transaction_id")
    @classmethod
    def _logical_id(cls, value: str) -> str:
        if _LOGICAL_RE.fullmatch(value) is None:
            raise ValueError("invalid logical browser transaction id")
        return value

    @model_validator(mode="after")
    def _transaction(self) -> ParkedBrowserTransaction:
        if _digest(self.envelope.model_dump(mode="json")) != self.envelope_digest:
            raise ValueError("browser transaction envelope digest mismatch")
        if (self.target_mode == "coordinate") != (self.coordinate is not None):
            raise ValueError("coordinate commits require coordinate fallback binding")
        if not self.binding.has_transaction_review_context():
            raise ValueError("parked browser transaction lacks trusted review context")
        return self


class ParkedProtectedDestinationApproval(_ParkedBrowserApproval):
    """One-execution approval for an exact protected fill destination."""

    kind: Literal["protected_destination"] = "protected_destination"
    protected_use: BrowserProtectedUseEvidence

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _DESTINATION_APPROVAL_RE.fullmatch(value) is None:
            raise ValueError("invalid protected destination approval id")
        return value


ParkedBrowserApproval = Annotated[
    ParkedBrowserTransaction | ParkedProtectedDestinationApproval,
    Field(discriminator="kind"),
]
_APPROVAL_FACTORY = TypeAdapter(ParkedBrowserApproval)


class BrowserTransactionChallenge(_FrozenModel):
    """One-time plaintext challenge returned only to the notifying owner."""

    approval: ParkedBrowserApproval
    code: str = Field(min_length=20, max_length=200)


class BrowserTransactionAttestation(_FrozenModel):
    """Append-only operator statement that never rewrites browser evidence."""

    id: int | None = Field(default=None, ge=1)
    transaction_id: str
    request_id: str = Field(pattern=r"^execution_[0-9a-f]{32}$")
    disposition: Literal["confirmed_completed", "confirmed_not_completed"]
    actor_principal_id: str = Field(min_length=1, max_length=500)
    source_conversation_id: str = Field(min_length=1, max_length=512)
    source_message_id: str = Field(min_length=1, max_length=512)
    note: str = Field(min_length=1, max_length=2_000)
    created_at: datetime

    @field_validator("transaction_id")
    @classmethod
    def _transaction_id(cls, value: str) -> str:
        if _TRANSACTION_RE.fullmatch(value) is None:
            raise ValueError("invalid parked browser transaction id")
        return value

    @field_validator("created_at")
    @classmethod
    def _created_at(cls, value: datetime) -> datetime:
        _utc(value, "created_at")
        return value


def envelope_digest(envelope: BrowserCommitEnvelope) -> str:
    return _digest(envelope.model_dump(mode="json"))


def challenge_digest(code: str) -> str:
    if len(code) < 20 or len(code) > 200 or any(character.isspace() for character in code):
        raise ValueError("browser approval code must be one bounded token")
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def issue_parked_approval(
    draft: BrowserApprovalDraft,
    *,
    code: str,
) -> ParkedBrowserApproval:
    """Bind a store-issued plaintext challenge without placeholder evidence."""

    payload = draft.model_dump(mode="python")
    payload.update(
        {
            "challenge_digest": challenge_digest(code),
            "state": "pending",
            "revision": 1,
            "decided_at": None,
            "decision_principal_id": None,
            "decision_source_message_id": None,
            "invalidation_reason": None,
        }
    )
    return _APPROVAL_FACTORY.validate_python(payload)


def _digest(value: object) -> str:
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_origins(values: tuple[str, ...]) -> tuple[str, ...]:
    canonical = tuple(_canonical_origin(value) for value in values)
    if len(canonical) != len(set(canonical)):
        raise ValueError("browser origins must be unique")
    return tuple(sorted(canonical))


def _canonical_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("browser origins must be canonical HTTPS origins")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("browser origin has an invalid port") from exc
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    canonical = f"https://{host}" + (f":{port}" if port not in {None, 443} else "")
    if value.rstrip("/") != canonical:
        raise ValueError("browser origin is not canonical")
    return canonical


def _canonical_plain_items(
    values: tuple[str, ...],
    *,
    name: str,
    pattern: str | None = None,
    max_length: int = 200,
) -> tuple[str, ...]:
    normalized = tuple(_plain(value, name, max_length) for value in values)
    if pattern is not None and any(re.fullmatch(pattern, value) is None for value in normalized):
        raise ValueError(f"invalid {name}")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must be unique")
    return tuple(sorted(normalized))


def _plain(value: str, name: str, max_length: int) -> str:
    if value != value.strip() or not value or len(value) > max_length:
        raise ValueError(f"{name} must be nonblank bounded text")
    if any(ord(character) < 32 and character not in "\t\n" for character in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
