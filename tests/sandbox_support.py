"""Test-only reversible capability used to exercise delegated-authority mechanics.

It performs no external effect: it writes one confined JSON record below the
isolated test user-data root. It is deliberately absent from Ricky's production
capability inventory and exists only to verify generic contracts and enforcement.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from ricky.authority.types import (
    AuthorityScope,
    AuthorityVerdict,
)
from ricky.capabilities.guardrails import (
    AuthenticatedSource,
    CollectedGuardrailField,
    CompiledGuardrail,
    GuardrailDecision,
    GuardrailFieldDecision,
    GuardrailFieldProposal,
    GuardrailIntakeField,
    GuardrailIntakeSpec,
    GuardrailUsage,
    GuardrailVerdict,
    compile_guardrail,
)
from ricky.config import RickySettings, user_data_path
from ricky.tools.base import EffectIdentity, EffectReceipt, Risk, ToolContext, ToolResult

CAPABILITY = "sandbox_reservation"
SCHEMA_ID = "sandbox.reservation"
SCHEMA_VERSION = 1
TOOL_NAME = "sandbox_reserve"
CONTRACT_CAPABILITY = "builtin.sandbox.reservation"


class SandboxReservationScope(BaseModel):
    """The normalized constraint schema owned by this capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    venue_id: str = Field(min_length=1, max_length=200)
    venue_name: str = Field(min_length=1, max_length=200)
    party_size: int = Field(ge=1, le=50)
    local_date: date
    window_start: time
    window_end: time
    timezone: str = Field(min_length=1, max_length=100)
    account_identity: str = Field(min_length=1, max_length=200)
    max_reservations: int = Field(default=1, ge=1, le=1)
    deposit_limit_minor: int = Field(default=0, ge=0, le=100_000_000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        return value.strip().upper() if value is not None else None

    @model_validator(mode="after")
    def _validate_window(self) -> SandboxReservationScope:
        if self.window_end <= self.window_start:
            raise ValueError("arrival window end must be after its start")
        if self.deposit_limit_minor > 0 and self.currency is None:
            raise ValueError("a deposit limit requires an explicit currency")
        return self

    def label(self) -> str:
        return (
            f"{self.venue_name} ({self.venue_id}) for {self.party_size} on "
            f"{self.local_date.isoformat()} between {self.window_start.isoformat('minutes')} "
            f"and {self.window_end.isoformat('minutes')} {self.timezone}"
        )


_REQUIRED_QUESTIONS: dict[str, str] = {
    "venue_id": "Which exact venue should I book? I need its exact identifier, not a name match.",
    "venue_name": "What is the venue's display name?",
    "party_size": "How many people is the reservation for?",
    "local_date": "Which local calendar date should I book (YYYY-MM-DD)?",
    "window_start": "What is the earliest acceptable arrival time (HH:MM)?",
    "window_end": "What is the latest acceptable arrival time (HH:MM)?",
    "timezone": "Which IANA timezone does that arrival window use?",
    "account_identity": "Which account identity should hold the reservation?",
}

_INTAKE_SPEC = GuardrailIntakeSpec(
    schema_id=SCHEMA_ID,
    schema_version=SCHEMA_VERSION,
    fields=(
        GuardrailIntakeField(
            name="venue_id",
            value_type="string",
            description="Exact machine-resolvable identifier for the venue.",
            question=_REQUIRED_QUESTIONS["venue_id"],
        ),
        GuardrailIntakeField(
            name="venue_name",
            value_type="string",
            description="Venue display name to show in the confirmation summary.",
            question=_REQUIRED_QUESTIONS["venue_name"],
        ),
        GuardrailIntakeField(
            name="party_size",
            value_type="integer",
            description="Exact number of people covered by the reservation.",
            question=_REQUIRED_QUESTIONS["party_size"],
            minimum=1,
            maximum=50,
        ),
        GuardrailIntakeField(
            name="local_date",
            value_type="date",
            description="Local calendar date for the reservation.",
            question=_REQUIRED_QUESTIONS["local_date"],
            format="YYYY-MM-DD",
        ),
        GuardrailIntakeField(
            name="window_start",
            value_type="time",
            description="Earliest acceptable local arrival time.",
            question=_REQUIRED_QUESTIONS["window_start"],
            format="HH:MM",
        ),
        GuardrailIntakeField(
            name="window_end",
            value_type="time",
            description="Latest acceptable local arrival time.",
            question=_REQUIRED_QUESTIONS["window_end"],
            format="HH:MM",
        ),
        GuardrailIntakeField(
            name="timezone",
            value_type="string",
            description="IANA timezone governing the local date and arrival window.",
            question=_REQUIRED_QUESTIONS["timezone"],
            format="IANA timezone",
        ),
        GuardrailIntakeField(
            name="account_identity",
            value_type="string",
            description="Exact account identity that may hold the reservation.",
            question=_REQUIRED_QUESTIONS["account_identity"],
        ),
        GuardrailIntakeField(
            name="deposit_limit_minor",
            value_type="integer",
            description="Maximum permitted deposit in minor currency units; zero forbids one.",
            required=False,
            question="What is the maximum permitted deposit in minor currency units?",
            minimum=0,
            maximum=100_000_000,
        ),
        GuardrailIntakeField(
            name="currency",
            value_type="string",
            description="Three-letter currency code required for a non-zero deposit limit.",
            required=False,
            question="Which three-letter currency applies to the deposit limit?",
            format="ISO 4217",
        ),
    ),
)


class SandboxReservationEvaluator:
    """Authority semantics for the sandbox reservation capability."""

    capability: ClassVar[str] = CAPABILITY
    schema_id: ClassVar[str] = SCHEMA_ID
    schema_version: ClassVar[int] = SCHEMA_VERSION
    tools: ClassVar[frozenset[str]] = frozenset({TOOL_NAME})
    scope_only_tools: ClassVar[frozenset[str]] = frozenset()
    uses_owner_financial_ceiling: ClassVar[bool] = False

    def summarize(self, scope: AuthorityScope) -> str:
        return self._summary(_scope(scope))

    def evaluate_call(
        self, scope: AuthorityScope, tool_name: str, args: dict[str, object]
    ) -> AuthorityVerdict:
        if tool_name != TOOL_NAME:
            return AuthorityVerdict(
                allowed=False, reason=f"{tool_name} is outside the sandbox reservation capability"
            )
        constraints = _scope(scope)
        try:
            call = SandboxReserveParams.model_validate(args)
        except ValueError as exc:
            return AuthorityVerdict(
                allowed=False, reason=f"invalid reservation call: {exc}"[:1_000]
            )
        if call.venue_id != constraints.venue_id:
            return AuthorityVerdict(allowed=False, reason="venue is outside the granted scope")
        if call.party_size != constraints.party_size:
            return AuthorityVerdict(allowed=False, reason="party size is outside the granted scope")
        if date.fromisoformat(call.local_date) != constraints.local_date:
            return AuthorityVerdict(allowed=False, reason="date is outside the granted scope")
        if call.timezone != constraints.timezone:
            return AuthorityVerdict(allowed=False, reason="timezone is outside the granted scope")
        arrival_time = time.fromisoformat(call.arrival_time)
        if not constraints.window_start <= arrival_time <= constraints.window_end:
            return AuthorityVerdict(
                allowed=False, reason="arrival time is outside the granted window"
            )
        if call.account_identity != constraints.account_identity:
            return AuthorityVerdict(allowed=False, reason="account is outside the granted scope")
        if call.deposit_minor > constraints.deposit_limit_minor:
            return AuthorityVerdict(allowed=False, reason="deposit exceeds the granted limit")
        if call.deposit_minor > 0 and call.currency != constraints.currency:
            return AuthorityVerdict(allowed=False, reason="deposit currency is outside the grant")
        return AuthorityVerdict(
            allowed=True,
            reason="within the granted reservation scope",
            amount_minor=call.deposit_minor,
            currency=constraints.currency if call.deposit_minor > 0 else None,
        )

    def effect_identity(
        self, scope: AuthorityScope, tool_name: str, args: dict[str, object]
    ) -> EffectIdentity:
        del tool_name, args
        constraints = _scope(scope)
        occurrence = (
            f"{constraints.local_date.isoformat()}:"
            f"{constraints.window_start.isoformat('minutes')}-"
            f"{constraints.window_end.isoformat('minutes')}:{constraints.timezone}"
        )
        payload = "\0".join(
            (
                CAPABILITY,
                constraints.venue_id,
                str(constraints.party_size),
                occurrence,
                constraints.account_identity,
            )
        )
        return EffectIdentity(
            operation=f"{CAPABILITY}.book",
            target=constraints.venue_id,
            occurrence=occurrence,
            summary=f"Sandbox reservation at {constraints.label()}",
            action_key=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )

    def receipt(self, scope: AuthorityScope, result: ToolResult) -> EffectReceipt:
        del scope
        if result.effect_receipt is not None:
            return result.effect_receipt
        # No typed receipt after dispatch means the outcome is unknown.
        return EffectReceipt(disposition="not_performed" if result.is_error else "in_doubt")

    def consumes_grant(self, scope: AuthorityScope, receipt: EffectReceipt) -> bool:
        del scope
        # max_reservations is 1: a performed or ambiguous booking ends the authority.
        return receipt.disposition in {"performed", "in_doubt"}

    def _summary(self, scope: SandboxReservationScope) -> str:
        deposit = (
            f"; deposit up to {scope.deposit_limit_minor} {scope.currency}"
            if scope.deposit_limit_minor > 0
            else "; no deposit"
        )
        return (
            f"Make at most {scope.max_reservations} sandbox reservation at {scope.label()} "
            f"as {scope.account_identity}{deposit}."
        )


class SandboxGuardrailEvaluator:
    """Generic contract guardrail view over the sandbox constraint schema."""

    capability_id: ClassVar[str] = CONTRACT_CAPABILITY
    schema_id: ClassVar[str] = SCHEMA_ID
    schema_version: ClassVar[int] = SCHEMA_VERSION
    tools: ClassVar[frozenset[str]] = frozenset({TOOL_NAME})
    intake_spec: ClassVar[GuardrailIntakeSpec] = _INTAKE_SPEC

    def normalize_field(
        self,
        proposal: GuardrailFieldProposal,
    ) -> GuardrailFieldDecision:
        field = self.intake_spec.get(proposal.field)
        if field is None:
            return GuardrailFieldDecision(
                accepted=False,
                reason=f"unknown sandbox reservation guardrail field: {proposal.field}",
            )
        try:
            value = _normalize_reservation_field(proposal.field, proposal.value)
        except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            return GuardrailFieldDecision(
                accepted=False,
                question=f"{field.question} The supplied value was invalid: {exc}"[:500],
            )
        return GuardrailFieldDecision(accepted=True, value=value)

    def validate_collected(
        self,
        fields: tuple[CollectedGuardrailField, ...],
        sources: tuple[AuthenticatedSource, ...],
    ) -> GuardrailDecision:
        source_by_id = {source.message_id: source for source in sources}
        raw: dict[str, JsonValue] = {}
        cited_ids: list[str] = []
        for item in fields:
            if (
                item.capability_id != self.capability_id
                or item.schema_id != self.schema_id
                or item.schema_version != self.schema_version
            ):
                return GuardrailDecision(reason="collected guardrail field has another identity")
            source = source_by_id.get(item.source_message_id)
            if source is None or source.text_digest != item.source_text_digest:
                return GuardrailDecision(
                    reason="collected guardrail field lacks authenticated turn provenance"
                )
            raw[item.field] = item.value
            if source.message_id not in cited_ids:
                cited_ids.append(source.message_id)
        missing = tuple(
            field.question
            for field in self.intake_spec.fields
            if field.required and raw.get(field.name) in (None, "")
        )
        if missing:
            return GuardrailDecision(questions=missing[:10])
        deposit_limit = raw.get("deposit_limit_minor", 0)
        if isinstance(deposit_limit, int) and deposit_limit > 0 and not raw.get("currency"):
            currency = self.intake_spec.get("currency")
            assert currency is not None
            return GuardrailDecision(questions=(currency.question,))
        try:
            constraints = SandboxReservationScope.model_validate(raw)
        except ValueError as exc:
            return GuardrailDecision(
                questions=(f"Those reservation details are not usable: {exc}"[:500],)
            )
        summary = SandboxReservationEvaluator()._summary(constraints)
        return GuardrailDecision(
            guardrail=compile_guardrail(
                capability_id=self.capability_id,
                schema_id=self.schema_id,
                schema_version=self.schema_version,
                constraints=constraints.model_dump(mode="json"),
                sources=tuple(source_by_id[source_id] for source_id in cited_ids),
                summary=summary,
            )
        )

    def summarize(self, guardrail: CompiledGuardrail) -> str:
        return _contract_scope(guardrail).label()

    def evaluate_call(
        self,
        guardrail: CompiledGuardrail,
        tool_name: str,
        args: dict[str, object],
        usage: GuardrailUsage,
    ) -> GuardrailVerdict:
        if usage.calls >= 1:
            return GuardrailVerdict(allowed=False, reason="reservation guardrail is exhausted")
        scope = AuthorityScope(
            capability=CAPABILITY,
            schema_id=guardrail.schema_id,
            schema_version=guardrail.schema_version,
            constraints=guardrail.constraints,
        )
        verdict = SandboxReservationEvaluator().evaluate_call(scope, tool_name, args)
        return GuardrailVerdict(
            allowed=verdict.allowed,
            reason=verdict.reason,
            usage_delta={"calls": 1} if verdict.allowed else {},
        )


def _normalize_reservation_field(name: str, value: JsonValue) -> JsonValue:
    if name in {"venue_id", "venue_name", "timezone", "account_identity", "currency"}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("a non-empty string is required")
        normalized = value.strip()
        if name == "currency":
            normalized = normalized.upper()
            if len(normalized) != 3:
                raise ValueError("currency must be a three-letter code")
        if name == "timezone":
            ZoneInfo(normalized)
        return normalized
    if name in {"party_size", "deposit_limit_minor"}:
        if isinstance(value, bool):
            raise ValueError("an integer is required")
        normalized = TypeAdapter(int).validate_python(value)
        upper = 50 if name == "party_size" else 100_000_000
        lower = 1 if name == "party_size" else 0
        if not lower <= normalized <= upper:
            raise ValueError(f"value must be between {lower} and {upper}")
        return normalized
    if name == "local_date":
        return TypeAdapter(date).validate_python(value).isoformat()
    if name in {"window_start", "window_end"}:
        normalized_time = TypeAdapter(time).validate_python(value)
        if normalized_time.tzinfo is not None:
            raise ValueError("time must be local and must not carry an offset")
        return normalized_time.isoformat("minutes")
    raise ValueError(f"unsupported reservation guardrail field: {name}")


def _contract_scope(guardrail: CompiledGuardrail) -> SandboxReservationScope:
    if (
        guardrail.capability_id != CONTRACT_CAPABILITY
        or guardrail.schema_id != SCHEMA_ID
        or guardrail.schema_version != SCHEMA_VERSION
        or not isinstance(guardrail.constraints, dict)
    ):
        raise ValueError("guardrail does not belong to the sandbox reservation capability")
    return SandboxReservationScope.model_validate(guardrail.constraints)


def _scope(scope: AuthorityScope) -> SandboxReservationScope:
    if scope.capability != CAPABILITY or scope.schema_id != SCHEMA_ID:
        raise ValueError("scope does not belong to the sandbox reservation capability")
    if scope.schema_version != SCHEMA_VERSION:
        raise ValueError("unsupported sandbox reservation scope version")
    if not isinstance(scope.constraints, dict):
        raise ValueError("sandbox reservation constraints must be an object")
    return SandboxReservationScope.model_validate(scope.constraints)


class SandboxReserveParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    venue_id: str = Field(min_length=1, max_length=200)
    party_size: int = Field(ge=1, le=50)
    local_date: str
    arrival_time: str
    timezone: str = Field(min_length=1, max_length=100)
    account_identity: str = Field(min_length=1, max_length=200)
    deposit_minor: int = Field(default=0, ge=0, le=100_000_000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        return value.strip().upper() if value is not None else None

    @field_validator("local_date")
    @classmethod
    def _local_date(cls, value: str) -> str:
        return date.fromisoformat(value).isoformat()

    @field_validator("arrival_time")
    @classmethod
    def _arrival_time(cls, value: str) -> str:
        parsed = time.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise ValueError("arrival_time must be local and must not carry an offset")
        return parsed.isoformat("minutes")


SandboxDispatch = Callable[[SandboxReserveParams], "SandboxOutcome"]


class SandboxOutcome(BaseModel):
    """The transport-level result of one sandbox booking attempt."""

    model_config = ConfigDict(extra="forbid")

    disposition: str = Field(pattern=r"^(performed|not_performed|in_doubt)$")
    reference: str | None = Field(default=None, max_length=200)
    detail: str = Field(min_length=1, max_length=1_000)


class SandboxReservationTool:
    """Write one confined, reversible sandbox reservation record."""

    name: ClassVar[str] = TOOL_NAME
    description: ClassVar[str] = (
        "Record one sandbox reservation. Reversible and local; it contacts no external service."
    )
    Params: ClassVar[type[BaseModel]] = SandboxReserveParams
    risk: ClassVar[Risk] = "mutating"
    capability_id = CONTRACT_CAPABILITY
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, settings: RickySettings, *, dispatch: SandboxDispatch | None = None) -> None:
        self.root = user_data_path(settings) / "sandbox-reservations"
        self._dispatch = dispatch or self._write

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = SandboxReserveParams.model_validate(params)
        outcome = self._dispatch(args)
        receipt = EffectReceipt(
            disposition=outcome.disposition,  # type: ignore[arg-type]
            provider_reference=outcome.reference,
        )
        return ToolResult(
            content=f"{outcome.disposition}: {outcome.detail}",
            is_error=outcome.disposition == "not_performed",
            effect_receipt=receipt,
        )

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        parsed = SandboxReserveParams.model_validate(args)
        encoded = parsed.model_dump_json()
        action_key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return EffectIdentity(
            operation="sandbox.reserve",
            target=parsed.venue_id,
            occurrence=action_key,
            summary=f"Reserve venue {parsed.venue_id}",
            action_key=action_key,
        )

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        return f"sandbox reservation: {json.dumps(args, sort_keys=True, default=str)[:500]}"

    def _write(self, args: SandboxReserveParams) -> SandboxOutcome:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        reference = hashlib.sha256(
            args.model_dump_json().encode("utf-8"),
        ).hexdigest()[:32]
        path = self.root / f"{reference}.json"
        payload = {
            **args.model_dump(mode="json"),
            "reference": reference,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        return SandboxOutcome(
            disposition="performed",
            reference=reference,
            detail=f"sandbox reservation recorded at {Path(path).name}",
        )
