"""Generic typed live-guardrail contracts and evaluator registry."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from ricky.capabilities.types import validate_capability_id


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthenticatedSource(_FrozenModel):
    """One authenticated user message allowed to source live constraints."""

    principal_id: str = Field(min_length=1, max_length=500)
    conversation_id: str = Field(min_length=1, max_length=512)
    message_id: str = Field(min_length=1, max_length=512)
    text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_snapshot: str = Field(min_length=1, max_length=20_000)
    received_at: datetime
    source_kind: Literal["authenticated_user"] = "authenticated_user"

    @model_validator(mode="after")
    def _source_time(self) -> AuthenticatedSource:
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("guardrail source time must be timezone-aware")
        if self.received_at.utcoffset() != timedelta(0):
            raise ValueError("guardrail source time must use UTC")
        return self


GuardrailValueType = Literal[
    "string",
    "integer",
    "number",
    "boolean",
    "date",
    "time",
    "datetime",
]


class GuardrailIntakeField(_FrozenModel):
    """One model-visible value accepted by a capability guardrail."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    value_type: GuardrailValueType
    description: str = Field(min_length=1, max_length=500)
    required: bool = True
    question: str = Field(min_length=1, max_length=500)
    format: str | None = Field(default=None, max_length=100)
    minimum: int | float | None = None
    maximum: int | float | None = None

    @model_validator(mode="after")
    def _bounds(self) -> GuardrailIntakeField:
        if self.minimum is not None and self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("guardrail intake maximum cannot be below minimum")
        return self


class GuardrailIntakeSpec(_FrozenModel):
    """Bounded capability-owned schema shown to the foreground model."""

    schema_id: str = Field(min_length=1, max_length=200)
    schema_version: int = Field(ge=1, le=1_000)
    fields: tuple[GuardrailIntakeField, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _fields(self) -> GuardrailIntakeSpec:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("guardrail intake field names must be unique")
        return self

    def get(self, name: str) -> GuardrailIntakeField | None:
        return next((field for field in self.fields if field.name == name), None)


class GuardrailFieldProposal(_FrozenModel):
    """One model-interpreted field plus optional non-authoritative audit context."""

    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    value: JsonValue
    source_quote: str | None = Field(
        default=None,
        max_length=4_000,
        description=(
            "Optional model-selected excerpt for audit/debugging only; it is not "
            "validated against the authenticated message and never gates compilation."
        ),
    )

    @model_validator(mode="after")
    def _quote(self) -> GuardrailFieldProposal:
        if self.source_quote is not None and not self.source_quote.strip():
            raise ValueError("guardrail field source quote cannot be blank")
        return self


class GuardrailProposal(_FrozenModel):
    """Model-proposed fields, still untrusted until source validation succeeds."""

    capability_id: str
    fields: Sequence[GuardrailFieldProposal] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _id(self) -> GuardrailProposal:
        validate_capability_id(self.capability_id)
        names = [field.field for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("a guardrail proposal cannot repeat a field")
        return self


class CollectedGuardrailField(_FrozenModel):
    """One normalized value bound to the authenticated turn that proposed it."""

    capability_id: str
    schema_id: str = Field(min_length=1, max_length=200)
    schema_version: int = Field(ge=1, le=1_000)
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    value: JsonValue
    source_message_id: str = Field(min_length=1, max_length=512)
    source_text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_quote: str | None = Field(
        default=None,
        max_length=4_000,
        description="Optional non-authoritative diagnostic excerpt retained from the proposal.",
    )

    @model_validator(mode="after")
    def _id(self) -> CollectedGuardrailField:
        validate_capability_id(self.capability_id)
        if self.source_quote is not None and not self.source_quote.strip():
            raise ValueError("collected guardrail field source quote cannot be blank")
        return self


class GuardrailFieldDecision(BaseModel):
    """Capability-owned structural normalization outcome for one proposed field."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    value: JsonValue = None
    question: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def _one_outcome(self) -> GuardrailFieldDecision:
        if self.accepted:
            if self.question is not None or self.reason is not None or self.value is None:
                raise ValueError("an accepted guardrail field requires only a normalized value")
        elif (self.question is None) == (self.reason is None):
            raise ValueError("a rejected guardrail field requires one question or reason")
        return self


class CompiledGuardrail(_FrozenModel):
    capability_id: str
    schema_id: str = Field(min_length=1, max_length=200)
    schema_version: int = Field(ge=1, le=1_000)
    constraints: JsonValue = None
    source_message_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    summary: str = Field(min_length=1, max_length=4_000)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _id(self) -> CompiledGuardrail:
        validate_capability_id(self.capability_id)
        if len(self.source_message_ids) != len(set(self.source_message_ids)):
            raise ValueError("guardrail source ids must be unique")
        return self


class GuardrailDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guardrail: CompiledGuardrail | None = None
    questions: tuple[str, ...] = Field(default=(), max_length=10)
    reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def _one_outcome(self) -> GuardrailDecision:
        outcomes = (
            int(self.guardrail is not None)
            + int(bool(self.questions))
            + int(self.reason is not None)
        )
        if outcomes != 1:
            raise ValueError("guardrail decision requires exactly one outcome")
        return self


class GuardrailUsage(_FrozenModel):
    calls: int = Field(default=0, ge=0)
    dimensions: dict[str, int] = Field(default_factory=dict)


class GuardrailVerdict(_FrozenModel):
    allowed: bool
    reason: str = Field(min_length=1, max_length=1_000)
    usage_delta: dict[str, int] = Field(default_factory=dict)


@runtime_checkable
class GuardrailEvaluator(Protocol):
    capability_id: ClassVar[str]
    schema_id: ClassVar[str]
    schema_version: ClassVar[int]
    tools: ClassVar[frozenset[str]]
    intake_spec: ClassVar[GuardrailIntakeSpec]

    def normalize_field(
        self,
        proposal: GuardrailFieldProposal,
    ) -> GuardrailFieldDecision: ...

    def validate_collected(
        self,
        fields: tuple[CollectedGuardrailField, ...],
        sources: tuple[AuthenticatedSource, ...],
    ) -> GuardrailDecision: ...

    def summarize(self, guardrail: CompiledGuardrail) -> str: ...

    def evaluate_call(
        self,
        guardrail: CompiledGuardrail,
        tool_name: str,
        args: dict[str, object],
        usage: GuardrailUsage,
    ) -> GuardrailVerdict: ...


class GuardrailRegistryError(RuntimeError):
    pass


class GuardrailRegistry:
    def __init__(self, evaluators: tuple[GuardrailEvaluator, ...] = ()) -> None:
        self._by_capability: dict[str, GuardrailEvaluator] = {}
        self._by_tool: dict[str, GuardrailEvaluator] = {}
        for evaluator in evaluators:
            self.register(evaluator)

    def register(self, evaluator: GuardrailEvaluator) -> None:
        validate_capability_id(evaluator.capability_id)
        if evaluator.capability_id in self._by_capability:
            raise GuardrailRegistryError(
                f"duplicate guardrail evaluator: {evaluator.capability_id}"
            )
        if not evaluator.tools:
            raise GuardrailRegistryError("a guardrail evaluator must govern at least one tool")
        intake = getattr(evaluator, "intake_spec", None)
        if not isinstance(intake, GuardrailIntakeSpec):
            raise GuardrailRegistryError("guardrail evaluator has no typed intake specification")
        if len(intake.model_dump_json()) > 16_000:
            raise GuardrailRegistryError("guardrail evaluator intake specification is too large")
        if (
            intake.schema_id != evaluator.schema_id
            or intake.schema_version != evaluator.schema_version
        ):
            raise GuardrailRegistryError(
                "guardrail evaluator intake schema identity does not match its evaluator"
            )
        for tool in evaluator.tools:
            if tool in self._by_tool:
                raise GuardrailRegistryError(f"tool has two guardrail evaluators: {tool}")
        self._by_capability[evaluator.capability_id] = evaluator
        for tool in evaluator.tools:
            self._by_tool[tool] = evaluator

    def get(self, capability_id: str) -> GuardrailEvaluator | None:
        return self._by_capability.get(capability_id)

    def for_tool(self, tool_name: str) -> GuardrailEvaluator | None:
        return self._by_tool.get(tool_name)

    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_capability))


def compile_guardrail(
    *,
    capability_id: str,
    schema_id: str,
    schema_version: int,
    constraints: JsonValue,
    sources: tuple[AuthenticatedSource, ...],
    summary: str,
) -> CompiledGuardrail:
    """Create a canonical immutable guardrail after evaluator validation."""

    source_ids = tuple(dict.fromkeys(source.message_id for source in sources))
    payload = {
        "capability_id": capability_id,
        "schema_id": schema_id,
        "schema_version": schema_version,
        "constraints": constraints,
        "source_message_ids": source_ids,
        "summary": summary,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return CompiledGuardrail(
        **payload,
        digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )
