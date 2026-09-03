"""Read-only retrieval of immutable tool-result artifacts."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ricky.agent.artifacts import SessionArtifactError, SessionArtifactStore, ToolArtifactChunk
from ricky.tools.base import Risk, ToolContext, ToolResult


class ReadToolArtifactParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")
    offset: int = Field(default=0, ge=0)
    max_chars: int | None = Field(default=None, ge=1)


class ReadToolArtifactTool:
    """Page through full text previously offloaded by this session."""

    name: ClassVar[str] = "read_tool_artifact"
    description: ClassVar[str] = (
        "Read a bounded character range from a large tool result previously offloaded "
        "in this session. Use only the opaque artifact id shown in the tool result."
    )
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.session.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None
    deferred_until_artifact: ClassVar[bool] = True
    result_is_bounded: ClassVar[bool] = True
    Params: ClassVar[type[BaseModel]] = ReadToolArtifactParams
    Result: ClassVar[type[BaseModel]] = ToolArtifactChunk

    def __init__(self, store: SessionArtifactStore) -> None:
        self._store = store

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        parsed = ReadToolArtifactParams.model_validate(params)
        try:
            chunk = await self._store.read(
                ctx.session,
                parsed.artifact_id,
                offset=parsed.offset,
                max_chars=parsed.max_chars,
            )
        except SessionArtifactError as exc:
            return ToolResult(content=str(exc), is_error=True)
        next_offset = "end" if chunk.next_offset is None else str(chunk.next_offset)
        return ToolResult(
            content=(
                f"artifact: {chunk.artifact_id}\n"
                f"range: {chunk.offset}:{chunk.end_offset} of {chunk.total_chars} chars\n"
                f"next offset: {next_offset}\n\n"
                f"{chunk.content}"
            ),
            data=chunk.model_dump(mode="json"),
        )
