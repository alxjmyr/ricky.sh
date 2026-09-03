"""Base tool contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ricky.agent.events import AgentEvent
from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.llm import ImagePart, ToolArtifactRef
from ricky.tool_contracts import EffectAttemptReason, EffectKind, Risk, UnattendedUse

if TYPE_CHECKING:
    # Annotation-only import: importing permissions at runtime would cycle back
    # through permissions.engine -> tools.base (Risk). Safe because
    # ``from __future__ import annotations`` defers annotation evaluation and
    # runtime_checkable only inspects method names, not signatures.
    from ricky.permissions.types import GrantScope

EffectDisposition = Literal["performed", "not_performed", "in_doubt"]
EventEmitter = Callable[[AgentEvent], Awaitable[None]]


class UserInteractionRequest(BaseModel):
    """Trusted exact prompt that returns control to the user after tool dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["guardrail_input", "confirmation"]
    correlation_id: str = Field(min_length=1, max_length=512)
    prompt: str = Field(min_length=1, max_length=8_000)


class EffectIdentity(BaseModel):
    """Deterministic occurrence identity a tool derives before dispatch.

    Lives beside ``EffectReceipt`` because both are tool-side contracts. Jobs,
    delegated agents, and workflows may consume the same identity without the
    tool knowing which runner coordinated it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=500)
    occurrence: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=2_000)
    action_key: str = Field(pattern=r"^[0-9a-f]{64}$")


def make_effect_identity(
    *,
    operation: str,
    target: str,
    occurrence: str,
    summary: str,
) -> EffectIdentity:
    """Build one canonical action key from non-secret deterministic identity facts."""

    encoded = json.dumps(
        {"operation": operation, "target": target, "occurrence": occurrence},
        sort_keys=True,
        separators=(",", ":"),
    )
    return EffectIdentity(
        operation=operation,
        target=target,
        occurrence=occurrence,
        summary=summary,
        action_key=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


class EffectReceipt(BaseModel):
    """Typed outcome evidence for one external mutation attempt."""

    disposition: EffectDisposition
    provider_reference: str | None = None
    attempt_reason: EffectAttemptReason | None = None
    """Typed pre-dispatch outcome; ordinary provider receipts leave this unset."""
    action_id: str | None = None
    """Durable per-attempt ledger identity assigned by an effect wrapper."""


class ToolResult(BaseModel):
    """A model-readable result returned by a tool invocation."""

    content: str
    data: JsonValue = None
    """Optional typed workflow data. Normal model transcripts use content only."""
    is_error: bool = False
    effect_receipt: EffectReceipt | None = None
    artifact: ToolArtifactRef | None = None
    full_content_chars: int | None = Field(default=None, ge=0)
    offload_error: str | None = None
    user_interaction: UserInteractionRequest | None = None
    follow_up_media: list[ImagePart] = Field(default_factory=list, max_length=20)
    """Canonical user-input media appended by the harness after this tool result."""


@runtime_checkable
class ToolArtifactAdmission(Protocol):
    """Stored record capable of projecting its provider-safe reference."""

    def reference(self) -> ToolArtifactRef:
        """Return reference metadata without a private storage locator."""
        ...


@runtime_checkable
class ToolArtifactSink(Protocol):
    """Narrow session-aware sink used only after a tool has executed once."""

    async def offload(
        self,
        session: AgentSession,
        *,
        call_id: str,
        tool_name: str,
        content: str,
        excerpt_chars: int,
    ) -> ToolArtifactAdmission:
        """Persist full text and return a record exposing ``reference()``."""
        ...


class ToolContext(BaseModel):
    """Runtime context passed to tools."""

    cwd: Path
    settings: RickySettings
    session: AgentSession
    emit_event: EventEmitter | None = None
    artifact_sink: ToolArtifactSink | None = None

    model_config = {"arbitrary_types_allowed": True}


class Tool(Protocol):
    """Callable surface implemented by tools available to the agent loop."""

    name: ClassVar[str]
    description: ClassVar[str]
    Params: ClassVar[type[BaseModel]]
    risk: ClassVar[Risk]

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        """Execute the tool with already-validated params."""
        ...


class DeclaredTool(Tool, Protocol):
    """Complete metadata surface required at every registration boundary.

    The strict runtime validator accepts structural implementations so legacy
    test doubles can still exercise lower-level dispatch in isolation. Tool
    authoring guidance lives in ``.designs/tool-authoring.md``.
    """

    @property
    def capability_id(self) -> str | None: ...

    @property
    def effect_kind(self) -> EffectKind: ...

    @property
    def unattended(self) -> UnattendedUse: ...

    @property
    def state_guard_id(self) -> str | None: ...


@runtime_checkable
class EffectIdentityProvider(Protocol):
    """External-effect tool that can identify an occurrence before dispatch."""

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        """Derive a deterministic identity without performing the effect."""
        ...


class PreparedEffect(Protocol):
    """Immutable, tool-owned payload binding identity to exact dispatch bytes."""

    @property
    def tool_name(self) -> str: ...

    @property
    def identity(self) -> EffectIdentity: ...

    @property
    def permission_summary(self) -> str | None: ...


@runtime_checkable
class PreparedEffectProvider(Protocol):
    """External-effect tool that prepares once before reservation and dispatch."""

    async def prepare_effect(self, args: dict[str, object], ctx: ToolContext) -> PreparedEffect:
        """Load and freeze exact effect input without performing the effect."""
        ...

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        """Dispatch exactly the already-prepared payload."""
        ...


@runtime_checkable
class PreparedEffectAborter(Protocol):
    """Prepared-effect owner that can unwind state when outer reservation stops."""

    async def abort_prepared(
        self,
        prepared: PreparedEffect,
        ctx: ToolContext,
        *,
        reason: str,
    ) -> None: ...


@runtime_checkable
class EffectActionBinder(Protocol):
    """Tool that correlates subsystem evidence with the shared action ledger."""

    def bind_effect_action(self, action_id: str, action_key: str) -> None: ...

    async def settle_effect_action(self, action_id: str) -> None: ...


@runtime_checkable
class StateGuard(Protocol):
    """Subsystem-owned wrapper for unattended Ricky-state mutations."""

    @property
    def id(self) -> str: ...

    def wrap(self, tool: Tool) -> Tool:
        """Return a tool enforcing the subsystem's unattended state contract."""
        ...


class StateGuardRegistry:
    """Small exact-id registry shared by unattended runners."""

    def __init__(self, guards: tuple[StateGuard, ...] | list[StateGuard] = ()) -> None:
        self._guards = {guard.id: guard for guard in guards}
        if len(self._guards) != len(guards):
            raise ValueError("state guard ids must be unique")

    def has(self, guard_id: str) -> bool:
        return guard_id in self._guards

    def wrap(self, guard_id: str, tool: Tool) -> Tool:
        guard = self._guards.get(guard_id)
        if guard is None:
            raise ValueError(f"unknown state guard: {guard_id}")
        return guard.wrap(tool)


@runtime_checkable
class PermissionArgsNormalizer(Protocol):
    """Optional hook that exposes effective policy arguments before permission checks."""

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        """Return policy arguments with tool-specific defaults resolved."""
        ...


@runtime_checkable
class PermissionSummarizer(Protocol):
    """Optional tool hook for a complete permission-review preview."""

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        """Describe the exact proposed side effect for the permission prompt."""
        ...


@runtime_checkable
class PermissionScoper(Protocol):
    """Optional tool hook declaring how a remembered grant may generalize.

    Returning ``None`` (or not implementing this) means the tool is not broadly
    grantable, so only allow-once / deny are offered for it.
    """

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope | None:
        """Return a grant scope for these args, or ``None`` if not grantable."""
        ...
