"""Strict, JSON-round-trip-safe contracts for task-scoped delegated authority.

The model *proposes* authority; deterministic code issues, narrows, rejects,
expires, and revokes it. Every model in this module is immutable once stored:
a grant is never edited to become wider.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from ricky.durable_tasks.types import validate_task_id
from ricky.executions.contracts import ConfirmationRef
from ricky.profiles import ProfileLabel, ProfileScope

GrantStatus = Literal["active", "revoked", "expired", "consumed"]
GrantActivityKind = Literal[
    "issued",
    "used",
    "denied",
    "revoked",
    "expired",
    "consumed",
]
EffectDisposition = Literal["performed", "not_performed", "in_doubt"]

_GRANT_ID = re.compile(r"^grant_[0-9a-f]{32}$")
_CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_SCHEMA_ID = re.compile(r"^[a-z0-9][a-z0-9_.]{0,99}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def validate_grant_id(value: str) -> str:
    if _GRANT_ID.fullmatch(value) is None:
        raise ValueError("invalid delegation grant id")
    return value


class AuthorityScope(_FrozenModel):
    """One capability-owned, versioned authority scope.

    ``constraints`` is opaque typed data selected by ``schema_id``; only the
    registered evaluator validates and interprets it.
    """

    capability: str
    schema_id: str
    schema_version: int = Field(ge=1, le=1_000)
    constraints: JsonValue = None

    @field_validator("capability")
    @classmethod
    def _capability(cls, value: str) -> str:
        if _CAPABILITY.fullmatch(value) is None:
            raise ValueError("invalid delegable capability name")
        return value

    @field_validator("schema_id")
    @classmethod
    def _schema_id(cls, value: str) -> str:
        if _SCHEMA_ID.fullmatch(value) is None:
            raise ValueError("invalid authority scope schema id")
        return value


class GrantSource(_FrozenModel):
    """The authenticated inbound user message that is the sole source of authority.

    A notification, model message, Web page, tool result, background report, or
    memory note can never populate this record.
    """

    principal_id: str = Field(min_length=1, max_length=500)
    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    sender_id: str = Field(min_length=1, max_length=500)
    destination_id: str = Field(min_length=1, max_length=500)
    platform_message_id: str = Field(min_length=1, max_length=500)
    inbound_message_id: str = Field(pattern=r"^inbound_[0-9a-f]{32}$")
    conversation_id: str = Field(pattern=r"^conversation_[0-9a-f]{32}$")
    text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_snapshot: str = Field(min_length=1, max_length=20_000)
    received_at: datetime

    @model_validator(mode="after")
    def _validate_source(self) -> GrantSource:
        _aware(self.received_at, "received_at")
        return self


def source_text_digest(text: str) -> str:
    """Exact digest of the source instruction text."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DelegationGrant(_FrozenModel):
    """Bounded authority for exactly one durable task and one execution request."""

    id: str
    source: GrantSource
    task_id: str
    task_revision: int = Field(ge=1)
    profile_scope: ProfileScope
    execution_request_id: str | None = Field(default=None, pattern=r"^execution_[0-9a-f]{32}$")
    contract_id: str = Field(pattern=r"^contract_[0-9a-f]{32}$")
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmations: tuple[ConfirmationRef, ...] = Field(default=(), max_length=20)
    scopes: tuple[AuthorityScope, ...] = Field(min_length=1, max_length=8)
    summary: str = Field(min_length=1, max_length=4_000)
    effect_call_limit: int = Field(ge=1, le=100)
    financial_limit_minor: int | None = Field(default=None, ge=0, le=100_000_000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    issued_at: datetime
    expires_at: datetime
    status: GrantStatus
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return validate_grant_id(value)

    @field_validator("task_id")
    @classmethod
    def _task(cls, value: str) -> str:
        return validate_task_id(value)

    @model_validator(mode="after")
    def _validate_grant(self) -> DelegationGrant:
        _aware(self.issued_at, "issued_at")
        _aware(self.expires_at, "expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("grant expires_at must be after issued_at")
        capabilities = [scope.capability for scope in self.scopes]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("a grant cannot carry two scopes for one capability")
        if (self.financial_limit_minor is None) != (self.currency is None):
            raise ValueError("a financial limit requires an explicit currency")
        return self

    def capabilities(self) -> frozenset[str]:
        return frozenset(scope.capability for scope in self.scopes)

    def scope_for(self, capability: str) -> AuthorityScope | None:
        return next(
            (scope for scope in self.scopes if scope.capability == capability),
            None,
        )

    def effect_namespace(self) -> str:
        """The shared effect-ledger namespace for this grant's durable task.

        Namespacing by task, not by grant, means a second grant for the same
        task cannot replay an action a first grant already performed.
        """

        return f"delegated:{self.task_id}"


class GrantActivity(_StrictModel):
    """One append-only authority audit record."""

    id: int = Field(ge=1)
    grant_id: str
    profile_label: ProfileLabel
    kind: GrantActivityKind
    capability: str | None = Field(default=None, max_length=64)
    tool_name: str | None = Field(default=None, max_length=100)
    action_id: str | None = Field(default=None, max_length=100)
    disposition: EffectDisposition | None = None
    summary: str = Field(min_length=1, max_length=2_000)
    created_at: datetime

    @field_validator("grant_id")
    @classmethod
    def _grant(cls, value: str) -> str:
        return validate_grant_id(value)

    @model_validator(mode="after")
    def _validate_activity(self) -> GrantActivity:
        _aware(self.created_at, "created_at")
        return self


class AuthorityVerdict(_StrictModel):
    """An evaluator's decision about one concrete effect tool call."""

    allowed: bool
    reason: str = Field(min_length=1, max_length=1_000)
    amount_minor: int = Field(default=0, ge=0, le=100_000_000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @model_validator(mode="after")
    def _validate_verdict(self) -> AuthorityVerdict:
        if self.amount_minor > 0 and self.currency is None:
            raise ValueError("a priced effect call must declare its currency")
        return self


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
