"""Additive structured ToolResult contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.tools import EffectReceipt, Risk, ToolContext, ToolRegistry, ToolResult


class EmptyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pass


class TypedResult(BaseModel):
    record_id: str
    count: float


class TypedTool:
    name: ClassVar[str] = "typed"
    description: ClassVar[str] = "Return typed data."
    Params: ClassVar[type[BaseModel]] = EmptyParams
    Result: ClassVar[type[BaseModel]] = TypedResult
    risk: ClassVar[Risk] = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, *, content: str, data: JsonValue) -> None:
        self.content = content
        self.data = data

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = params, ctx
        return ToolResult(content=self.content, data=self.data)


class InvalidExternalResultTool(TypedTool):
    name: ClassVar[str] = "typed_external"
    risk: ClassVar[Risk] = "mutating"
    effect_kind = "external"

    def __init__(
        self,
        *,
        invalid_data: JsonValue,
        disposition: Literal["performed", "not_performed", "in_doubt"] = "performed",
    ) -> None:
        super().__init__(content="unused", data=invalid_data)
        self.disposition: Literal["performed", "not_performed", "in_doubt"] = disposition

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = params, ctx
        return ToolResult(
            content="provider accepted the mutation",
            data=self.data,
            effect_receipt=EffectReceipt(
                disposition=self.disposition,
                provider_reference="provider-123",
            ),
        )


def _context(tmp_path: Path) -> ToolContext:
    settings = RickySettings()
    return ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )


async def test_registry_preserves_typed_data_when_display_text_is_truncated(
    tmp_path: Path,
) -> None:
    data = {"record_id": "r-1", "count": 3}
    registry = ToolRegistry([TypedTool(content="x" * 100, data=data)], max_result_chars=20)

    result = await registry.dispatch("typed", {}, _context(tmp_path))

    assert result.content.endswith("[truncated]")
    assert result.data == data
    assert result.is_error is False


async def test_display_text_change_does_not_change_structured_data(tmp_path: Path) -> None:
    data = {"record_id": "r-1", "count": 3}
    first = await ToolRegistry([TypedTool(content="first", data=data)]).dispatch(
        "typed", {}, _context(tmp_path)
    )
    second = await ToolRegistry([TypedTool(content="second", data=data)]).dispatch(
        "typed", {}, _context(tmp_path)
    )

    assert first.content != second.content
    assert first.data == second.data == data


async def test_missing_or_wrong_typed_data_fails_closed(tmp_path: Path) -> None:
    missing = await ToolRegistry([TypedTool(content="none", data=None)]).dispatch(
        "typed", {}, _context(tmp_path)
    )
    wrong = await ToolRegistry(
        [TypedTool(content="wrong", data={"record_id": "r-1", "count": "three"})]
    ).dispatch("typed", {}, _context(tmp_path))

    assert missing.is_error and missing.data is None
    assert "no structured data" in missing.content
    assert wrong.is_error and wrong.data is None
    assert "invalid structured data" in wrong.content


async def test_non_json_number_in_typed_data_fails_closed(tmp_path: Path) -> None:
    result = await ToolRegistry(
        [TypedTool(content="nan", data={"record_id": "r-1", "count": float("nan")})]
    ).dispatch("typed", {}, _context(tmp_path))

    assert result.is_error is True
    assert result.data is None
    assert "non-JSON structured data" in result.content


async def test_result_validation_error_preserves_performed_effect_evidence(
    tmp_path: Path,
) -> None:
    result = await ToolRegistry(
        [InvalidExternalResultTool(invalid_data={"record_id": "r-1", "count": "invalid"})]
    ).dispatch("typed_external", {}, _context(tmp_path))

    assert result.is_error
    assert result.data is None
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "performed"
    assert result.effect_receipt.provider_reference == "provider-123"


async def test_all_effect_dispositions_survive_every_result_shape_failure(
    tmp_path: Path,
) -> None:
    failures: tuple[JsonValue, ...] = (
        None,
        {"record_id": "r-1", "count": "invalid"},
        {"record_id": "r-1", "count": float("nan")},
    )
    for disposition in ("performed", "not_performed", "in_doubt"):
        for invalid_data in failures:
            result = await ToolRegistry(
                [
                    InvalidExternalResultTool(
                        disposition=disposition,
                        invalid_data=invalid_data,
                    )
                ]
            ).dispatch("typed_external", {}, _context(tmp_path))

            assert result.is_error
            assert result.data is None
            assert result.effect_receipt is not None
            assert result.effect_receipt.disposition == disposition
            assert result.effect_receipt.provider_reference == "provider-123"
