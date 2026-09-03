"""Browser-owned live guard boundary for background execution runtimes."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, field_validator, model_validator

from ricky.browser.types import (
    BrowserActionKind,
    BrowserFailure,
    BrowserModel,
    BrowserSessionMode,
    BrowserTransactionEvidence,
    CoordinateFallbackEvidence,
)
from ricky.profiles import ProfileResourceRef

BrowserToolName = Literal[
    "browser_resources",
    "browser_session_open",
    "browser_session_open_resource",
    "browser_session_close",
    "browser_pages",
    "browser_page_select",
    "browser_navigate",
    "browser_scroll",
    "browser_snapshot",
    "browser_visual_snapshot",
    "browser_click",
    "browser_fill",
    "browser_fill_protected",
    "browser_select",
    "browser_set_checked",
    "browser_press_key",
    "browser_commit",
    "browser_upload",
    "browser_download",
    "browser_coordinate_click",
    "browser_coordinate_commit",
]
BrowserBudgetKind = Literal[
    "session_starts",
    "navigations",
    "scrolls",
    "semantic_observations",
    "visual_observations",
    "interactions",
    "protected_materializations",
    "uploads",
    "upload_bytes",
    "downloads",
    "download_bytes",
    "transaction_commits",
    "created_pages",
    "controlled_pages",
]
BrowserRuntimeDisposition = Literal[
    "completed",
    "not_performed",
    "performed",
    "in_doubt",
]


class BrowserGuardFacts(BrowserModel):
    """Bounded safe live facts checked independently of model arguments."""

    tool_name: BrowserToolName
    resource: ProfileResourceRef | None = None
    resource_configuration_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    session_id: str | None = Field(
        default=None,
        pattern=r"^browser_session_[0-9a-f]{32}$",
    )
    session_mode: BrowserSessionMode | None = None
    headless: bool | None = None
    page_id: str | None = Field(
        default=None,
        pattern=r"^browser_page_[0-9a-f]{32}$",
    )
    navigation_generation: int | None = Field(default=None, ge=0)
    controlled_page_count: int = Field(default=0, ge=0, le=100)
    top_level_origin: str | None = Field(default=None, max_length=500)
    target_frame_origin: str | None = Field(default=None, max_length=500)
    effective_destination_origins: tuple[str, ...] = Field(default=(), max_length=20)
    private_destination_origins: tuple[str, ...] = Field(default=(), max_length=22)
    provider: str | None = Field(default=None, min_length=1, max_length=200)
    snapshot_id: str | None = Field(
        default=None,
        pattern=r"^browser_snapshot_[0-9a-f]{32}$",
    )
    target_ref: str | None = Field(
        default=None,
        pattern=r"^(?:(?:f[0-9]+)?e[0-9]+|d[0-9]+)$",
        max_length=100,
    )
    action_kind: BrowserActionKind | None = None
    attachment_count: int = Field(default=0, ge=0, le=100)
    attachment_ids: tuple[str, ...] = Field(default=(), max_length=100)
    attachment_sha256: tuple[str, ...] = Field(default=(), max_length=100)
    byte_count: int = Field(default=0, ge=0)
    transaction: BrowserTransactionEvidence | None = None
    coordinate_fallback: CoordinateFallbackEvidence | None = None
    protected_resource: ProfileResourceRef | None = None
    protected_revision: int | None = Field(default=None, ge=1)
    protected_field: str | None = Field(default=None, max_length=64)

    @field_validator("attachment_sha256")
    @classmethod
    def _attachment_digests(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in values
        ):
            raise ValueError("attachment digests must be lowercase SHA-256 values")
        return values

    @model_validator(mode="after")
    def _coherent_coordinate_fallback(self) -> BrowserGuardFacts:
        if self.coordinate_fallback is not None and self.tool_name not in {
            "browser_coordinate_click",
            "browser_coordinate_commit",
        }:
            raise ValueError("coordinate fallback evidence belongs only to coordinate clicks")
        if self.transaction is not None and self.tool_name not in {
            "browser_commit",
            "browser_coordinate_commit",
        }:
            raise ValueError("transaction evidence belongs only to browser commit tools")
        if self.attachment_count != len(self.attachment_sha256):
            raise ValueError("attachment count must match exact attachment digests")
        if self.attachment_ids and len(self.attachment_ids) != self.attachment_count:
            raise ValueError("attachment ids must match the attachment count")
        if self.attachment_sha256 and self.tool_name != "browser_upload":
            raise ValueError("attachment digests belong only to browser upload")
        protected = (
            self.protected_resource is not None
            or self.protected_revision is not None
            or self.protected_field is not None
        )
        if protected != (self.tool_name == "browser_fill_protected"):
            raise ValueError("protected resource facts belong only to protected fill")
        if self.tool_name == "browser_fill_protected" and (
            self.protected_resource is None or self.protected_field is None
        ):
            raise ValueError("protected fill guard facts require resource and field")
        observed_origins = {
            item
            for item in (
                self.top_level_origin,
                self.target_frame_origin,
                *self.effective_destination_origins,
            )
            if item is not None
        }
        if not set(self.private_destination_origins) <= observed_origins:
            raise ValueError("private destination facts must identify observed origins")
        return self


class BrowserRuntimeEvidence(BrowserModel):
    """Safe outcome evidence emitted after one guarded browser operation."""

    facts: BrowserGuardFacts
    disposition: BrowserRuntimeDisposition
    action_id: str | None = Field(
        default=None,
        pattern=r"^browser_action_[0-9a-f]{32}$",
    )
    failure: BrowserFailure | None = None
    result_byte_count: int = Field(default=0, ge=0)
    created_page_count: int = Field(default=0, ge=0, le=50)

    @model_validator(mode="after")
    def _coherent_failure(self) -> BrowserRuntimeEvidence:
        if self.disposition == "completed" and self.failure is not None:
            raise ValueError("completed browser runtime evidence cannot carry a failure")
        return self


@runtime_checkable
class BrowserExecutionGuard(Protocol):
    """Execution-owned adapter used by the browser at every live boundary.

    ``check`` revalidates immutable contract, authority, claim, and live facts.
    ``reserve`` atomically consumes one cumulative budget before work begins.
    ``record`` persists only the safe evidence model after an outcome is known.
    """

    @property
    def execution_id(self) -> str: ...

    @property
    def private_origin_ceiling(self) -> tuple[str, ...]: ...

    @property
    def controlled_page_ceiling(self) -> int: ...

    def bind_effect_action(self, action_id: str, action_key: str) -> None:
        """Bind subsequent effect evidence to the shared durable action."""

        ...

    async def settle_effect_action(self, action_id: str) -> None:
        """Publish bound evidence after the shared action is resolved."""

        ...

    async def check(self, facts: BrowserGuardFacts) -> None: ...

    async def reserve(
        self,
        kind: BrowserBudgetKind,
        amount: int,
        facts: BrowserGuardFacts,
    ) -> None: ...

    async def reserve_possible_pages(
        self,
        maximum_creation_count: int,
        facts: BrowserGuardFacts,
    ) -> None:
        """Reserve worst-case popup capacity, settled by the resulting evidence."""

        ...

    async def release_controlled_pages(
        self,
        amount: int,
        facts: BrowserGuardFacts,
    ) -> None:
        """Release confirmed simultaneous page ownership after closure or settlement."""

        ...

    async def record(self, evidence: BrowserRuntimeEvidence) -> None: ...
