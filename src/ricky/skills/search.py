"""Bounded literal text search inside the active passive skill bundle."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ricky.skills.registry import SkillRegistry
from ricky.tools.base import Risk, ToolContext, ToolResult

_TEXT_SUFFIXES = {".md", ".txt", ".toml", ".example", ".json", ".yaml", ".yml"}
_ENTRY_LIMIT = 1_000
_BYTE_LIMIT = 2_000_000
_FILE_BYTE_LIMIT = 250_000


class SearchSkillResourcesParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=200, description="Literal, case-insensitive text.")
    path: str | None = Field(default=None, description="Optional bundle-relative text file.")
    offset: int = Field(default=0, ge=0, le=10_000, description="Matching lines to skip.")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum matching lines to return.")

    @field_validator("query")
    @classmethod
    def _nonblank_query(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("query must be nonblank text on one line")
        return value


class SkillSearchMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    line: int
    text: str


class SkillSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    matches: list[SkillSearchMatch]
    has_more: bool
    incomplete: bool


class SearchSkillResourcesTool:
    """Search resources without requiring general host filesystem authority."""

    name: ClassVar[str] = "search_skill_resources"
    description: ClassVar[str] = (
        "Search text in the active skill bundle with a literal, case-insensitive query. "
        "Returns file paths, 1-based line numbers, and excerpts for read_skill_resource. "
        "Searches Markdown, text, TOML, example, JSON, and YAML files; skips symlinks."
    )
    Params: ClassVar[type[BaseModel]] = SearchSkillResourcesParams
    Result: ClassVar[type[BaseModel]] = SkillSearchResult
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.skill.use"
    effect_kind = "none"
    unattended = "allowed"
    review_mode = "policy"
    state_guard_id = None

    def __init__(self, skill_registry: SkillRegistry) -> None:
        self._skill_registry = skill_registry

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = SearchSkillResourcesParams.model_validate(params)
        active = ctx.session.active_skill
        if active is None:
            return ToolResult(content="No skill is active; activate a skill first.", is_error=True)
        try:
            # The registry validates the active identity against its current or pinned bundle.
            self._skill_registry.resolve_resource(active, "SKILL.md")
            assert active.bundle_path is not None
            root = Path(active.bundle_path).resolve()
            if args.path is not None:
                selected = self._skill_registry.resolve_resource(active, args.path)
                paths = [selected.relative_to(root)]
                incomplete = False
            else:
                paths, incomplete = _text_paths(root)
        except (ValueError, OSError) as exc:
            return ToolResult(content=str(exc), is_error=True)

        matches: list[SkillSearchMatch] = []
        remaining_bytes = _BYTE_LIMIT
        skipped = 0
        has_more = False
        pattern = re.compile(re.escape(args.query), re.IGNORECASE)
        for relative in paths:
            await asyncio.sleep(0)
            if remaining_bytes <= 0:
                incomplete = True
                break
            try:
                path = self._skill_registry.resolve_resource(active, relative.as_posix())
                allowed_bytes = min(_FILE_BYTE_LIMIT, remaining_bytes)
                with path.open("rb") as stream:
                    raw = stream.read(allowed_bytes)
                remaining_bytes -= len(raw)
                if path.stat().st_size > len(raw):
                    incomplete = True
                    continue
                body = raw.decode("utf-8")
            except (ValueError, OSError, UnicodeError):
                incomplete = True
                continue
            if "\0" in body:
                incomplete = True
                continue
            for line_number, line in enumerate(body.splitlines(), start=1):
                match = pattern.search(line)
                if match is None:
                    continue
                if skipped < args.offset:
                    skipped += 1
                    continue
                if len(matches) == args.limit:
                    has_more = True
                    break
                start = max(0, match.start() - 80)
                excerpt = line[start : start + 400]
                matches.append(
                    SkillSearchMatch(
                        path=relative.as_posix(),
                        line=line_number,
                        text=("…" if start else "")
                        + excerpt
                        + ("…" if start + 400 < len(line) else ""),
                    )
                )
            if has_more:
                break

        result = SkillSearchResult(matches=matches, has_more=has_more, incomplete=incomplete)
        content = "\n".join(f"{m.path}:{m.line}: {m.text}" for m in matches) or "No matches."
        if has_more:
            content += (
                f"\nMore matches: use offset={args.offset + len(matches)} or narrow the path."
            )
        if incomplete:
            content += (
                "\nSearch incomplete: a scan limit or unreadable/non-text file was encountered."
            )
        return ToolResult(content=content, data=result.model_dump(mode="json"))


def _text_paths(root: Path) -> tuple[list[Path], bool]:
    paths: list[Path] = []
    entries = 0
    incomplete = False

    def onerror(error: OSError) -> None:
        nonlocal incomplete
        incomplete = True

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        entries += len(dirs) + len(files)
        if entries > _ENTRY_LIMIT:
            return sorted(paths), True
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs if not (parent / name).is_symlink())
        paths.extend(
            (parent / name).relative_to(root)
            for name in sorted(files)
            if Path(name).suffix.lower() in _TEXT_SUFFIXES and not (parent / name).is_symlink()
        )
    return sorted(paths), incomplete
