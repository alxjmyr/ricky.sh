"""Provider-safe protected-value models and private material containers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from ricky.profiles import ProfileResourceRef

ProtectedValueKind = Literal["credential", "payment_card", "one_time", "generic"]
MaterializationMode = Literal["stored", "prompt_each_use"]
DestinationPolicyMode = Literal["strict", "confirm_new", "approved_only", "secure_web"]
ExecutionMode = Literal["foreground", "unattended"]
ProtectedCommitDisposition = Literal[
    "reserved",
    "performed",
    "not_performed",
    "in_doubt",
]
ProtectedUseDisposition = Literal["reserved", "materialized", "cancelled", "failed"]
ProtectedControlKind = Literal[
    "username",
    "password",
    "one_time_code",
    "cardholder_name",
    "card_number",
    "card_expiry_month",
    "card_expiry_year",
    "card_expiry",
    "card_security_code",
    "generic_secret",
]

_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONSUMER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class ProtectedFieldDescriptor(BaseModel):
    """Safe metadata for one field inside a protected resource."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    mode: MaterializationMode
    compatible_controls: tuple[ProtectedControlKind, ...] = Field(min_length=1, max_length=20)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if _FIELD_RE.fullmatch(value) is None:
            raise ValueError("protected field names must be lowercase identifiers")
        return value

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("protected field labels cannot be blank")
        return value

    @field_validator("compatible_controls")
    @classmethod
    def _unique_controls(
        cls, values: tuple[ProtectedControlKind, ...]
    ) -> tuple[ProtectedControlKind, ...]:
        if len(values) != len(set(values)):
            raise ValueError("compatible protected controls must be unique")
        return values


class ProtectedDestinationPolicy(BaseModel):
    """Destination and foreground/unattended ceilings for one resource."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: DestinationPolicyMode = "strict"
    authored_origins: tuple[str, ...] = Field(default=(), max_length=100)
    foreground_allowed: bool = True
    unattended_allowed: bool = False
    max_unattended_materializations_per_execution: int = Field(default=0, ge=0, le=1_000)
    unattended_commit_allowed: bool = False
    max_unattended_commits_per_execution: int = Field(default=0, ge=0, le=1_000)
    max_unattended_amount_minor: int = Field(default=0, ge=0, le=100_000_000)
    unattended_currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("authored_origins")
    @classmethod
    def _origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("protected destination origins cannot be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("protected destination origins must be unique")
        return normalized

    @field_validator("unattended_currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("unattended currency must be a three-letter code")
        return normalized

    @model_validator(mode="after")
    def _coherent_unattended_policy(self) -> ProtectedDestinationPolicy:
        if not self.unattended_allowed and self.max_unattended_materializations_per_execution:
            raise ValueError("forbidden unattended use cannot have a materialization allowance")
        if not self.unattended_commit_allowed and (
            self.max_unattended_commits_per_execution or self.max_unattended_amount_minor
        ):
            raise ValueError("forbidden unattended commit cannot have commit allowances")
        if self.max_unattended_amount_minor > 0 and self.unattended_currency is None:
            raise ValueError("an unattended amount ceiling requires a currency")
        return self


class ProtectedValueDescriptor(BaseModel):
    """Safe catalog record; every field is provider-visible by design."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ref: ProfileResourceRef
    kind: ProtectedValueKind
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1_000)
    fields: tuple[ProtectedFieldDescriptor, ...] = Field(min_length=1, max_length=50)
    policy: ProtectedDestinationPolicy
    revision: int = Field(ge=1)
    enabled: bool = True
    created_at: datetime
    updated_at: datetime

    @field_validator("label", "description")
    @classmethod
    def _safe_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _unique_fields(self) -> ProtectedValueDescriptor:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("protected field names must be unique")
        return self

    def field(self, name: str) -> ProtectedFieldDescriptor:
        for field in self.fields:
            if field.name == name:
                return field
        raise ValueError(f"protected field is not available: {name}")


class ProtectedDestinationApproval(BaseModel):
    """Durable exact origin-pair approval without protected material."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ref: ProfileResourceRef
    top_level_origin: str = Field(min_length=1, max_length=500)
    frame_origin: str = Field(min_length=1, max_length=500)
    approved_at: datetime


class ProtectedOccurrenceBinding(BaseModel):
    """Consumer-owned safe binding for one reviewed destination occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    generation: int = Field(ge=0)
    observation_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=200)


class ProtectedUseRequest(BaseModel):
    """Trusted local facts for one proposed materialization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ref: ProfileResourceRef
    field: str = Field(min_length=1, max_length=64)
    consumer_id: str = Field(min_length=1, max_length=128)
    control_kind: ProtectedControlKind
    top_level_origin: str = Field(min_length=1, max_length=500)
    frame_origin: str = Field(min_length=1, max_length=500)
    occurrence: str = Field(min_length=1, max_length=500)
    approval_binding: ProtectedOccurrenceBinding | None = None
    execution_mode: ExecutionMode = "foreground"
    execution_id: str | None = Field(
        default=None,
        pattern=r"^execution_[0-9a-f]{32}$",
    )

    @field_validator("field")
    @classmethod
    def _field_name(cls, value: str) -> str:
        if _FIELD_RE.fullmatch(value) is None:
            raise ValueError("protected field names must be lowercase identifiers")
        return value

    @field_validator("consumer_id")
    @classmethod
    def _consumer_id(cls, value: str) -> str:
        if _CONSUMER_RE.fullmatch(value) is None:
            raise ValueError("protected consumer id is invalid")
        return value

    @model_validator(mode="after")
    def _execution_binding(self) -> ProtectedUseRequest:
        if self.execution_mode == "foreground" and self.execution_id is not None:
            raise ValueError("foreground protected use cannot name an execution")
        if self.execution_mode == "unattended" and self.execution_id is None:
            raise ValueError("unattended protected use requires an execution id")
        return self


class ProtectedUseRecord(BaseModel):
    """Durable safe evidence for one broker use occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^protected_use_[0-9a-f]{32}$")
    request: ProtectedUseRequest
    resource_revision: int = Field(ge=1)
    disposition: ProtectedUseDisposition
    created_at: datetime
    finalized_at: datetime | None = None


class ProtectedCommitRequest(BaseModel):
    """Safe exact facts for one unattended commit using a protected resource."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_id: str = Field(pattern=r"^execution_[0-9a-f]{32}$")
    ref: ProfileResourceRef
    revision: int = Field(ge=1)
    fields: tuple[str, ...] = Field(min_length=1, max_length=100)
    logical_effect_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    envelope_kind: Literal["browser", "financial"]
    amount_minor: int | None = Field(default=None, ge=0, le=100_000_000_000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @field_validator("fields")
    @classmethod
    def _commit_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("protected commit fields must be unique")
        if any(_FIELD_RE.fullmatch(value) is None for value in values):
            raise ValueError("protected commit fields must be lowercase identifiers")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def _amount_binding(self) -> ProtectedCommitRequest:
        financial = self.envelope_kind == "financial"
        if financial != (self.amount_minor is not None and self.currency is not None):
            raise ValueError("financial protected commits require exact amount and currency")
        return self


class ProtectedCommitRecord(BaseModel):
    """Durable reservation and conservative outcome for one protected-source commit."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^protected_commit_[0-9a-f]{32}$")
    request: ProtectedCommitRequest
    disposition: ProtectedCommitDisposition
    created_at: datetime
    finalized_at: datetime | None = None

    @model_validator(mode="after")
    def _finalization(self) -> ProtectedCommitRecord:
        if (self.disposition == "reserved") != (self.finalized_at is None):
            raise ValueError("only reserved protected commits omit finalization time")
        return self


class SecureValueInputRequest(BaseModel):
    """Trusted no-echo prompt request containing safe metadata only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ref: ProfileResourceRef
    field: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    top_level_origin: str = Field(min_length=1, max_length=500)
    frame_origin: str = Field(min_length=1, max_length=500)


class UnlockRequest(BaseModel):
    """Trusted vault-unlock prompt request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: str = Field(min_length=1, max_length=64)


class DestinationApprovalRequest(BaseModel):
    """Trusted confirmation for an exact new destination pair."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ref: ProfileResourceRef
    revision: int = Field(ge=1)
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=200)
    top_level_origin: str = Field(min_length=1, max_length=500)
    frame_origin: str = Field(min_length=1, max_length=500)
    occurrence: str = Field(min_length=1, max_length=500)
    binding: ProtectedOccurrenceBinding | None = None
    execution_mode: ExecutionMode


class DestinationApprovalResponse(BaseModel):
    """Fail-closed local response for a new destination."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: Literal["deny", "allow_once", "approve"]


class ProtectedVaultStatus(BaseModel):
    """Safe operator-visible lifecycle state for one profile vault."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: str = Field(min_length=1, max_length=64)
    enabled: bool
    initialized: bool
    unlocked: bool
    resource_count: int = Field(ge=0)


@dataclass(frozen=True, repr=False)
class ProtectedMaterial:
    """Private exact material carried only between broker and trusted consumer."""

    use: ProtectedUseRecord
    descriptor: ProtectedValueDescriptor
    field: ProtectedFieldDescriptor
    value: SecretStr
    authorization: Literal["authored", "approved", "allow_once", "secure_web"]


@dataclass(frozen=True, repr=False)
class StoredSecretPayload:
    """Decrypted private fields inside one exact resource revision."""

    values: dict[str, SecretStr]
