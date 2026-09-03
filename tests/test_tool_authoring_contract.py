"""Contract checks and reusable authoring-harness coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.tools import (
    EffectReceipt,
    Risk,
    ToolContext,
    ToolContractError,
    ToolRegistry,
    ToolResult,
    make_effect_identity,
)
from ricky.tools.testing import assert_tool_contract


class NestedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


class StrictParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: str
    payload: NestedValue


class ReadTool:
    name: ClassVar[str] = "contract_read"
    description: ClassVar[str] = "Read one value for a contract test."
    Params: ClassVar[type[BaseModel]] = StrictParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.sample.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = StrictParams.model_validate(params)
        return ToolResult(content=f"{parsed.target}:{parsed.payload.label}")


class ExternalTool(ReadTool):
    name: ClassVar[str] = "contract_send"
    description: ClassVar[str] = "Send one value for a contract test."
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.sample.mutate"
    effect_kind = "external"

    def __init__(self, *, receipt: bool = True) -> None:
        self.calls = 0
        self.receipt = receipt

    def effect_identity(self, args: dict[str, object], ctx: ToolContext):
        del ctx
        parsed = StrictParams.model_validate(args)
        return make_effect_identity(
            operation=self.name,
            target=parsed.target,
            occurrence=parsed.payload.label,
            summary="Send one contract-test value",
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        self.calls += 1
        return ToolResult(
            content="sent",
            effect_receipt=(
                EffectReceipt(disposition="performed", provider_reference="fake-1")
                if self.receipt
                else None
            ),
        )


def _ctx(tmp_path: Path) -> ToolContext:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
    )
    return ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )


@pytest.mark.asyncio
async def test_reusable_contract_harness_checks_read_tool_and_provider_normalization(
    tmp_path: Path,
) -> None:
    result = await assert_tool_contract(
        ReadTool(),
        valid_args={"target": "one", "payload": {"label": "two"}},
        provider_args={"target": "one", "payload": '{"label":"two"}'},
        expected_normalized_paths=("payload",),
        ctx=_ctx(tmp_path),
    )

    assert result.content == "one:two"


@pytest.mark.asyncio
async def test_reusable_contract_harness_checks_external_identity_and_receipt(
    tmp_path: Path,
) -> None:
    tool = ExternalTool()

    result = await assert_tool_contract(
        tool,
        valid_args={"target": "recipient", "payload": {"label": "occurrence"}},
        ctx=_ctx(tmp_path),
    )

    assert result.effect_receipt is not None
    assert result.effect_receipt.provider_reference == "fake-1"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_reusable_contract_harness_rejects_loose_params_and_missing_receipt(
    tmp_path: Path,
) -> None:
    class LooseParams(BaseModel):
        value: str

    class LooseTool(ReadTool):
        Params: ClassVar[type[BaseModel]] = LooseParams

    with pytest.raises(ToolContractError, match="strict=True"):
        await assert_tool_contract(
            LooseTool(),
            valid_args={"value": "ok"},
            ctx=_ctx(tmp_path),
        )

    with pytest.raises(AssertionError, match="contract error"):
        await assert_tool_contract(
            ExternalTool(receipt=False),
            valid_args={"target": "recipient", "payload": {"label": "occurrence"}},
            ctx=_ctx(tmp_path),
        )


def test_registry_rejects_invalid_callable_surfaces_at_construction() -> None:
    class InvalidName(ReadTool):
        name: ClassVar[str] = "Invalid Name"

    class EmptyDescription(ReadTool):
        description: ClassVar[str] = "  "

    class SyncRun(ReadTool):
        def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
            del params, ctx
            return ToolResult(content="wrong")

    for tool in (InvalidName(), EmptyDescription(), SyncRun()):
        with pytest.raises(ToolContractError):
            ToolRegistry([cast(Any, tool)])


def test_registry_rejects_non_model_params_and_results_at_construction() -> None:
    class InvalidParams(ReadTool):
        Params: ClassVar[Any] = dict

    class InvalidResult(ReadTool):
        Result: ClassVar[Any] = dict

    for tool in (InvalidParams(), InvalidResult()):
        with pytest.raises(ToolContractError, match="Pydantic BaseModel"):
            ToolRegistry([cast(Any, tool)])
