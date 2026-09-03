"""Provider-safe protected-value catalog tool."""

from __future__ import annotations

import json
from typing import ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ricky.profiles import ProfileResourceRef
from ricky.protected_values.service import ProtectedValueBroker
from ricky.protected_values.types import ProtectedValueKind
from ricky.tool_contracts import Risk
from ricky.tools import ToolContext, ToolResult


class ProtectedValuesCatalogParams(BaseModel):
    """Bounded safe catalog filters."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: ProtectedValueKind | None = None
    ref: str | None = Field(default=None, min_length=3, max_length=300)
    limit: int | None = Field(default=None, ge=1, le=1_000)


class ProtectedValuesCatalogTool:
    """List aliases and policy metadata without unlocking protected payloads."""

    name: ClassVar[str] = "protected_values_catalog"
    description: ClassVar[str] = (
        "List safe protected-value aliases, fields, and destination policies available "
        "in the current profile scope. This never reveals stored values."
    )
    Params: ClassVar[type[BaseModel]] = ProtectedValuesCatalogParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.protected_value.read"
    effect_kind: ClassVar[Literal["none"]] = "none"
    unattended: ClassVar[Literal["allowed"]] = "allowed"
    state_guard_id = None
    contract_version = 1

    def __init__(self, broker: ProtectedValueBroker) -> None:
        self._broker = broker

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = ProtectedValuesCatalogParams.model_validate(params)
        ref = ProfileResourceRef.from_qualified(parsed.ref) if parsed.ref is not None else None
        descriptors = await self._broker.catalog(
            kind=parsed.kind,
            ref=ref,
            limit=parsed.limit,
        )
        payload = [
            {
                **descriptor.model_dump(mode="json", exclude={"ref"}),
                "ref": descriptor.ref.qualified,
            }
            for descriptor in descriptors
        ]
        return ToolResult(
            content=json.dumps(payload, sort_keys=True),
            data=cast(JsonValue, payload),
        )
