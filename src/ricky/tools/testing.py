"""Reusable behavioral contract checks for tool-author unit tests."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import cast

from ricky.tool_contracts import inspect_tool_contract
from ricky.tools.base import (
    EffectIdentity,
    EffectIdentityProvider,
    Tool,
    ToolContext,
    ToolResult,
)
from ricky.tools.registry import ToolRegistry


async def assert_tool_contract(
    tool: Tool,
    *,
    valid_args: Mapping[str, object],
    ctx: ToolContext,
    provider_args: Mapping[str, object] | None = None,
    expected_normalized_paths: Iterable[str] = (),
    secret_values: Iterable[str] = (),
) -> ToolResult:
    """Exercise one tool's shared schema, normalization, identity, and result contract.

    Use only with a fake client or an otherwise isolated implementation: the valid
    invocation is dispatched once so external receipt behavior can be verified.
    """

    metadata = inspect_tool_contract(tool)
    registry = ToolRegistry([tool])
    prepared = registry.prepare_args(tool.name, valid_args)
    assert prepared.error is None, _prepared_failure(prepared.error)
    assert prepared.args is not None

    params = tool.Params.model_validate(prepared.args, strict=True)
    json_args = json.loads(params.model_dump_json(round_trip=True))
    round_tripped = registry.prepare_args(tool.name, json_args)
    assert round_tripped.error is None, _prepared_failure(round_tripped.error)
    assert round_tripped.args == prepared.args

    probe_key = "__ricky_contract_probe__"
    while probe_key in valid_args:
        probe_key = f"_{probe_key}"
    probe_value = "ricky-contract-value-must-not-be-echoed"
    unexpected = registry.prepare_args(
        tool.name,
        {**dict(valid_args), probe_key: probe_value},
    )
    assert unexpected.error is not None, f"{tool.name} Params must set ConfigDict(extra='forbid')"
    assert unexpected.error.is_error
    assert unexpected.error.data is not None
    assert probe_value not in unexpected.error.content

    if provider_args is not None:
        provider_prepared = registry.prepare_args(tool.name, provider_args)
        assert provider_prepared.error is None, _prepared_failure(provider_prepared.error)
        assert provider_prepared.args == prepared.args
        assert provider_prepared.normalized_paths == tuple(expected_normalized_paths)

    secrets = tuple(value for value in secret_values if value)
    if metadata.effect_kind == "external":
        identity_provider = cast(EffectIdentityProvider, tool)
        first = EffectIdentity.model_validate(identity_provider.effect_identity(prepared.args, ctx))
        second = EffectIdentity.model_validate(
            identity_provider.effect_identity(prepared.args, ctx)
        )
        assert first == second, f"{tool.name} effect identity is not deterministic"
        identity_text = first.model_dump_json()
        for secret in secrets:
            assert secret not in identity_text, f"{tool.name} effect identity contains a secret"

    result = await registry.dispatch(tool.name, valid_args, ctx, call_id="contract_probe")
    assert not result.is_error, result.content
    if metadata.effect_kind == "external":
        assert result.effect_receipt is not None
        assert result.effect_receipt.disposition in {
            "performed",
            "not_performed",
            "in_doubt",
        }
    elif metadata.risk == "read_only":
        assert result.effect_receipt is None, (
            f"read-only tool {tool.name} must not return an effect receipt"
        )
    result_text = result.model_dump_json()
    for secret in secrets:
        assert secret not in result_text, f"{tool.name} result contains a secret"
    return result


def _prepared_failure(error: ToolResult | None) -> str:
    return "argument preparation unexpectedly failed" if error is None else error.content
