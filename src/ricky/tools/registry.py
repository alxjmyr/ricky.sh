"""Explicit tool registry and dispatch boundary."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, JsonValue, ValidationError

from ricky.llm import ToolArtifactRef, ToolSpec
from ricky.tool_contracts import ReviewMode, inspect_tool_review_mode, inspect_tool_surface
from ricky.tools.arguments import normalize_arguments
from ricky.tools.base import (
    EffectReceipt,
    PermissionArgsNormalizer,
    PermissionScoper,
    PermissionSummarizer,
    PreparedEffect,
    PreparedEffectProvider,
    Tool,
    ToolContext,
    ToolResult,
)

if TYPE_CHECKING:
    from ricky.permissions.types import GrantScope

DEFAULT_MAX_RESULT_CHARS = 12_000


@dataclass(frozen=True)
class PreparedToolArguments:
    """Canonical validated arguments or one model-readable rejection."""

    args: dict[str, object] | None
    normalized_paths: tuple[str, ...] = ()
    error: ToolResult | None = None


class ToolRegistry:
    """A side-effect-free registry of explicitly provided tools."""

    def __init__(
        self,
        tools: list[Tool] | tuple[Tool, ...],
        *,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    ) -> None:
        review_modes: dict[str, ReviewMode] = {}
        for tool in tools:
            inspect_tool_surface(tool)
            review_modes[tool.name] = inspect_tool_review_mode(tool)
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")
        self._max_result_chars = max_result_chars
        self._review_modes = review_modes

    def get(self, name: str) -> Tool | None:
        """Return a registered tool by name."""
        return self._tools.get(name)

    def tools(self) -> list[Tool]:
        """Return registered tools in deterministic insertion order."""
        return list(self._tools.values())

    def review_mode(self, name: str) -> ReviewMode:
        """Return one registered tool's validated interactive-review mode."""

        return self._review_modes.get(name, "policy")

    def prepare_args(
        self,
        name: str,
        args: Mapping[str, object],
        *,
        parse_error: str | None = None,
    ) -> PreparedToolArguments:
        """Normalize and validate one call before policy or execution."""

        tool = self.get(name)
        if tool is None:
            return PreparedToolArguments(
                args=None,
                error=ToolResult(
                    content=f"Unknown tool: {name}. Choose a tool from the provided catalog.",
                    data={"error": "unknown_tool", "tool": name},
                    is_error=True,
                ),
            )
        if parse_error is not None:
            return PreparedToolArguments(
                args=None,
                error=ToolResult(
                    content=(
                        f"Malformed arguments for {name}: the provider returned {parse_error}. "
                        "Reissue the tool call with one valid JSON object."
                    ),
                    data={"error": "malformed_json", "tool": name},
                    is_error=True,
                ),
            )
        normalized_paths: tuple[str, ...] = ()
        try:
            normalized = normalize_arguments(tool.Params, args)
            normalized_paths = normalized.paths
            params = tool.Params.model_validate(normalized.args, strict=True)
        except ValidationError as exc:
            details = _validation_details(exc, args)
            return PreparedToolArguments(
                args=None,
                normalized_paths=normalized_paths,
                error=ToolResult(
                    content=_render_validation_error(name, details),
                    data=cast(
                        JsonValue,
                        {"error": "invalid_arguments", "tool": name, "details": details},
                    ),
                    is_error=True,
                ),
            )
        canonical = params.model_dump(mode="python", round_trip=True)
        if not isinstance(canonical, dict):
            raise TypeError("tool parameter models must serialize to a JSON object")
        return PreparedToolArguments(args=canonical, normalized_paths=normalized.paths)

    def validate_args(self, name: str, args: Mapping[str, object]) -> ToolResult | None:
        """Return a model-readable error when tool arguments are invalid."""

        return self.prepare_args(name, args).error

    def specs(self) -> list[ToolSpec]:
        """Return provider-neutral tool specs for context assembly."""
        return [
            ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=tool.Params.model_json_schema(),
            )
            for tool in self._tools.values()
        ]

    def permission_args(
        self, name: str, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        """Resolve effective arguments used only by the permission path."""
        tool = self.get(name)
        if tool is None or not isinstance(tool, PermissionArgsNormalizer):
            return dict(args)
        return tool.normalize_permission_args(args, ctx)

    def permission_summary(
        self, name: str, args: dict[str, object], ctx: ToolContext
    ) -> str | None:
        """Return a tool-declared permission preview when one is available."""
        tool = self.get(name)
        if tool is None or not isinstance(tool, PermissionSummarizer):
            return None
        return tool.summarize_permission(args, ctx)

    def permission_scope(
        self, name: str, args: dict[str, object], ctx: ToolContext
    ) -> GrantScope | None:
        """Return a tool-declared grant scope when the tool opts in."""
        tool = self.get(name)
        if tool is None or not isinstance(tool, PermissionScoper):
            return None
        return tool.permission_scope(args, ctx)

    async def dispatch(
        self,
        name: str,
        args: Mapping[str, object],
        ctx: ToolContext,
        *,
        call_id: str | None = None,
    ) -> ToolResult:
        """Validate arguments, run a tool, and enforce harness result limits."""
        prepared = self.prepare_args(name, args)
        if prepared.error is not None:
            return prepared.error
        tool = self.get(name)
        assert tool is not None and prepared.args is not None
        params = tool.Params.model_validate(prepared.args, strict=True)

        try:
            result = await tool.run(params, ctx)
        except Exception as exc:  # noqa: BLE001 - tool failures become model-readable results.
            result = ToolResult(content=f"{name} failed: {exc}", is_error=True)
        return await self._finish_result(
            tool,
            result,
            tool_name=name,
            call_id=call_id,
            ctx=ctx,
        )

    async def dispatch_prepared(
        self,
        name: str,
        args: Mapping[str, object],
        prepared_effect: PreparedEffect,
        ctx: ToolContext,
        *,
        call_id: str | None = None,
    ) -> ToolResult:
        """Dispatch the exact immutable effect prepared before permission review."""
        prepared = self.prepare_args(name, args)
        if prepared.error is not None:
            return prepared.error
        tool = self.get(name)
        assert tool is not None and prepared.args is not None
        if not isinstance(tool, PreparedEffectProvider):
            return ToolResult(
                content=f"{name} does not support prepared dispatch",
                is_error=True,
            )
        if prepared_effect.tool_name != name:
            return ToolResult(
                content=f"prepared effect belongs to {prepared_effect.tool_name}, not {name}",
                is_error=True,
                effect_receipt=EffectReceipt(disposition="not_performed"),
            )
        params = tool.Params.model_validate(prepared.args, strict=True)
        try:
            result = await tool.run_prepared(params, prepared_effect, ctx)
        except Exception as exc:  # noqa: BLE001 - tool failures become model-readable results.
            result = ToolResult(content=f"{name} failed: {exc}", is_error=True)
        return await self._finish_result(
            tool,
            result,
            tool_name=name,
            call_id=call_id,
            ctx=ctx,
        )

    async def _finish_result(
        self,
        tool: Tool,
        result: ToolResult,
        *,
        tool_name: str,
        call_id: str | None,
        ctx: ToolContext,
    ) -> ToolResult:
        """Apply the shared result contract after ordinary or prepared dispatch."""
        validated = self._validate_result(tool, result)
        if getattr(tool, "result_is_bounded", False):
            return validated
        return await self._bound_result(
            validated,
            tool_name=tool_name,
            call_id=call_id,
            ctx=ctx,
        )

    @staticmethod
    def _validate_result(tool: Tool, result: ToolResult) -> ToolResult:
        if getattr(tool, "effect_kind", None) == "external" and result.effect_receipt is None:
            return result.model_copy(
                update={
                    "content": (
                        f"{result.content}\n"
                        f"[contract error: {tool.name} returned no external-effect receipt]"
                    ),
                    "is_error": True,
                    "effect_receipt": EffectReceipt(disposition="in_doubt"),
                }
            )
        result_model: type[BaseModel] | None = getattr(tool, "Result", None)
        if result_model is None or result.is_error:
            return result
        if result.data is None:
            return result.model_copy(
                update={
                    "content": f"{tool.name} returned no structured data for its Result model",
                    "is_error": True,
                    "data": None,
                }
            )
        try:
            validated = result_model.model_validate(result.data)
            data = validated.model_dump(mode="json")
            data = json.loads(json.dumps(data, allow_nan=False))
        except ValidationError as exc:
            return result.model_copy(
                update={
                    "content": f"{tool.name} returned invalid structured data: {exc}",
                    "is_error": True,
                    "data": None,
                }
            )
        except (TypeError, ValueError) as exc:
            return result.model_copy(
                update={
                    "content": f"{tool.name} returned non-JSON structured data: {exc}",
                    "is_error": True,
                    "data": None,
                }
            )
        return result.model_copy(update={"data": data})

    def _truncate(self, result: ToolResult) -> ToolResult:
        if len(result.content) <= self._max_result_chars:
            return result
        marker = "\n[truncated]"
        return result.model_copy(
            update={
                "content": _bounded_suffix(result.content, self._max_result_chars, marker),
                "full_content_chars": len(result.content),
            }
        )

    async def _bound_result(
        self,
        result: ToolResult,
        *,
        tool_name: str,
        call_id: str | None,
        ctx: ToolContext,
    ) -> ToolResult:
        policy = ctx.settings.context.tool_results
        threshold = min(policy.offload_threshold_chars, self._max_result_chars)
        if len(result.content) <= threshold:
            return result
        if not policy.enabled or ctx.artifact_sink is None or call_id is None:
            return self._truncate(result)

        excerpt = _excerpt(result.content, policy.inline_excerpt_chars, policy.head_fraction)
        try:
            record = await ctx.artifact_sink.offload(
                ctx.session,
                call_id=call_id,
                tool_name=tool_name,
                content=result.content,
                excerpt_chars=len(excerpt.head) + len(excerpt.tail),
            )
        except Exception as exc:  # noqa: BLE001 - preserve the successful tool effect.
            marker = "\n[non-recoverably truncated]"
            return result.model_copy(
                update={
                    "content": _bounded_suffix(
                        result.content,
                        self._max_result_chars,
                        marker,
                    ),
                    "full_content_chars": len(result.content),
                    "offload_error": type(exc).__name__,
                }
            )

        reference = record.reference()
        visible = _render_offloaded(tool_name, reference, excerpt)
        return result.model_copy(
            update={
                "content": visible,
                "artifact": reference,
                "full_content_chars": len(result.content),
            }
        )


def _validation_details(
    error: ValidationError,
    args: Mapping[str, object],
) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        location = tuple(str(part) for part in item["loc"])
        received = _value_at(args, location)
        details.append(
            {
                "path": ".".join(location) or "$",
                "message": str(item["msg"]),
                "expected": str(item["type"]),
                "received": "missing" if received is _MISSING else type(received).__name__,
            }
        )
    return details


def _render_validation_error(name: str, details: list[dict[str, str]]) -> str:
    lines = [f"Invalid arguments for {name}:"]
    lines.extend(
        f"- {item['path']}: {item['message']} "
        f"(expected {item['expected']}; received {item['received']})"
        for item in details[:20]
    )
    if len(details) > 20:
        lines.append(f"- [{len(details) - 20} additional validation errors omitted]")
    lines.append("Correct the arguments and retry the tool call.")
    return "\n".join(lines)


_MISSING = object()


def _value_at(value: object, location: tuple[str, ...]) -> object:
    current = value
    for part in location:
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


class _Excerpt:
    def __init__(self, head: str, tail: str, omitted: int) -> None:
        self.head = head
        self.tail = tail
        self.omitted = omitted


def _excerpt(content: str, budget: int, head_fraction: float) -> _Excerpt:
    excerpt_chars = min(len(content), budget)
    head_chars = math.floor(excerpt_chars * head_fraction)
    tail_chars = excerpt_chars - head_chars
    head = content[:head_chars]
    tail = content[len(content) - tail_chars :] if tail_chars else ""
    return _Excerpt(head, tail, len(content) - len(head) - len(tail))


def _render_offloaded(tool_name: str, reference: ToolArtifactRef, excerpt: _Excerpt) -> str:
    artifact_id = reference.id
    full_chars = reference.full_chars
    digest = reference.sha256
    return (
        "[tool result offloaded]\n"
        f"artifact: {artifact_id}\n"
        f"tool: {tool_name}\n"
        f"full size: {full_chars:,} chars\n"
        f"sha256: {digest}\n\n"
        "<head excerpt>\n"
        f"{excerpt.head}\n"
        f"[omitted {excerpt.omitted:,} chars; use read_tool_artifact]\n"
        "<tail excerpt>\n"
        f"{excerpt.tail}"
    )


def _bounded_suffix(content: str, limit: int, marker: str) -> str:
    if limit <= 0:
        return ""
    if len(marker) >= limit:
        return marker[-limit:]
    return f"{content[: limit - len(marker)]}{marker}"
