"""Permission-aware host file tools."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ricky.permissions.types import GrantScope
from ricky.tools.base import EffectReceipt, Risk, ToolContext, ToolResult, make_effect_identity
from ricky.tools.paths import (
    display_path,
    is_workspace_path,
    resolve_host_glob,
    resolve_host_path,
)


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(description="Absolute, home-relative, or workspace-relative file path.")
    offset: int = Field(default=1, ge=1, description="First 1-based line number to include.")
    limit: int = Field(default=200, ge=1, le=1000, description="Maximum lines to include.")


class ReadFileTool:
    name: ClassVar[str] = "read_file"
    description: ClassVar[str] = "Read a host file with 1-based line numbers."
    Params: ClassVar[type[BaseModel]] = ReadFileParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.project.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        return {**args, "path": str(resolve_host_path(ctx.cwd, str(args.get("path", ""))))}

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        path = resolve_host_path(ctx.cwd, str(args.get("path", "")))
        return _read_scope(
            ctx,
            params_equal={"path": str(path)},
            label=f"read_file at {path}",
            path=path,
            directory=path.parent,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ReadFileParams.model_validate(params)
        path = resolve_host_path(ctx.cwd, args.path)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = args.offset - 1
        selected = lines[start : start + args.limit]
        content = "\n".join(f"{start + index + 1}: {line}" for index, line in enumerate(selected))
        return ToolResult(content=content or "[empty]")


class WriteFileParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(description="Absolute, home-relative, or workspace-relative output path.")
    content: str = Field(description="Complete file content to write.")


class WriteFileTool:
    name: ClassVar[str] = "write_file"
    description: ClassVar[str] = "Create or overwrite a host file with exact content."
    Params: ClassVar[type[BaseModel]] = WriteFileParams
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.project.mutate"
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        return {**args, "path": str(resolve_host_path(ctx.cwd, str(args.get("path", ""))))}

    def effect_identity(self, args: dict[str, object], ctx: ToolContext):
        path = resolve_host_path(ctx.cwd, str(args.get("path", "")))
        if not path.parent.exists():
            raise ValueError(f"parent directory does not exist: {path.parent}")
        return make_effect_identity(
            operation=self.name,
            target=str(path),
            occurrence=f"{ctx.session.id}:{_sha256(str(args.get('content', '')))}",
            summary=f"Write {display_path(ctx.cwd, path)}",
        )

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope | None:
        path = str(resolve_host_path(ctx.cwd, str(args.get("path", ""))))
        return GrantScope(params_equal={"path": path}, label=f"write_file at {path}")

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = WriteFileParams.model_validate(params)
        path = resolve_host_path(ctx.cwd, args.path)
        if not path.parent.exists():
            return ToolResult(
                content=f"Parent directory does not exist: {path.parent}", is_error=True
            )
        path.write_text(args.content, encoding="utf-8")
        return ToolResult(
            content=f"Wrote {display_path(ctx.cwd, path)} ({len(args.content)} chars)",
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=str(path)),
        )


class EditFileParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(description="Absolute, home-relative, or workspace-relative file path.")
    old: str = Field(description="Exact text to replace. Must occur exactly once.")
    new: str = Field(description="Replacement text.")


class EditFileTool:
    name: ClassVar[str] = "edit_file"
    description: ClassVar[str] = "Replace one exact string occurrence in a host file."
    Params: ClassVar[type[BaseModel]] = EditFileParams
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.project.mutate"
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        return {**args, "path": str(resolve_host_path(ctx.cwd, str(args.get("path", ""))))}

    def effect_identity(self, args: dict[str, object], ctx: ToolContext):
        path = resolve_host_path(ctx.cwd, str(args.get("path", "")))
        text = path.read_text(encoding="utf-8", errors="replace")
        old = str(args.get("old", ""))
        count = text.count(old)
        if count != 1:
            raise ValueError(f"expected exactly one match for old text, found {count}")
        return make_effect_identity(
            operation=self.name,
            target=str(path),
            occurrence=(f"{ctx.session.id}:{_sha256(old + chr(0) + str(args.get('new', '')))}"),
            summary=f"Edit {display_path(ctx.cwd, path)}",
        )

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope | None:
        path = str(resolve_host_path(ctx.cwd, str(args.get("path", ""))))
        return GrantScope(params_equal={"path": path}, label=f"edit_file at {path}")

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = EditFileParams.model_validate(params)
        path = resolve_host_path(ctx.cwd, args.path)
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(args.old)
        if count != 1:
            return ToolResult(
                content=f"Expected exactly one match for old text, found {count}.",
                is_error=True,
            )
        updated = text.replace(args.old, args.new, 1)
        path.write_text(updated, encoding="utf-8")
        return ToolResult(
            content=f"Edited {display_path(ctx.cwd, path)}",
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=str(path)),
        )


class ListDirParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(
        default=".",
        description="Absolute, home-relative, or workspace-relative directory path.",
    )
    include_hidden: bool = False


class ListDirTool:
    name: ClassVar[str] = "list_dir"
    description: ClassVar[str] = "List one host directory shallowly."
    Params: ClassVar[type[BaseModel]] = ListDirParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.project.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        return {**args, "path": str(resolve_host_path(ctx.cwd, str(args.get("path", "."))))}

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        path = resolve_host_path(ctx.cwd, str(args.get("path", ".")))
        return _read_scope(
            ctx,
            params_equal={"path": str(path)},
            label=f"list_dir at {path}",
            path=path,
            directory=path,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ListDirParams.model_validate(params)
        path = resolve_host_path(ctx.cwd, args.path)
        entries = []
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            if not args.include_hidden and child.name.startswith("."):
                continue
            suffix = "/" if child.is_dir() else ""
            entries.append(f"{child.name}{suffix}")
        return ToolResult(content="\n".join(entries) or "[empty]")


class GlobSearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pattern: str = Field(description="Absolute, home-relative, or workspace-relative glob pattern.")
    include_hidden: bool = False
    max_results: int = Field(default=200, ge=1, le=1000)


class GlobSearchTool:
    name: ClassVar[str] = "glob_search"
    description: ClassVar[str] = "Find host paths matching a glob pattern."
    Params: ClassVar[type[BaseModel]] = GlobSearchParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.project.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        resolved = resolve_host_glob(ctx.cwd, str(args.get("pattern", "")))
        return {
            **args,
            "pattern": resolved.canonical_pattern,
            # Permission-only effective root used by directory grants. It is
            # intentionally absent from GlobSearchParams and never dispatched.
            "path": str(resolved.root),
        }

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        resolved = resolve_host_glob(ctx.cwd, str(args.get("pattern", "")))
        root = resolve_host_path(ctx.cwd, str(args.get("path", resolved.root)))
        return _read_scope(
            ctx,
            params_equal={
                "pattern": resolved.canonical_pattern,
            },
            label=f"glob_search at {resolved.canonical_pattern}",
            path=root,
            directory=root,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GlobSearchParams.model_validate(params)
        resolved_glob = resolve_host_glob(ctx.cwd, args.pattern)
        matches = []
        for path in sorted(resolved_glob.root.glob(resolved_glob.pattern)):
            resolved = path.resolve()
            if not resolved.is_relative_to(resolved_glob.root):
                continue
            rel = display_path(ctx.cwd, resolved)
            scoped = resolved.relative_to(resolved_glob.root)
            if not args.include_hidden and any(part.startswith(".") for part in scoped.parts):
                continue
            matches.append(rel)
            if len(matches) >= args.max_results:
                break
        return ToolResult(content="\n".join(matches) or "[no matches]")


class GrepSearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    regex: str = Field(description="Python regular expression to search for.")
    path: str = Field(
        default=".",
        description="Absolute, home-relative, or workspace-relative file or directory.",
    )
    include_hidden: bool = False
    max_results: int = Field(default=200, ge=1, le=1000)


class GrepSearchTool:
    name: ClassVar[str] = "grep_search"
    description: ClassVar[str] = "Search host text files for a regex and return matches."
    Params: ClassVar[type[BaseModel]] = GrepSearchParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.project.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        return {**args, "path": str(resolve_host_path(ctx.cwd, str(args.get("path", "."))))}

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        path = resolve_host_path(ctx.cwd, str(args.get("path", ".")))
        directory = path if path.is_dir() else path.parent
        return _read_scope(
            ctx,
            params_equal={"path": str(path)},
            label=f"grep_search at {path}",
            path=path,
            directory=directory,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GrepSearchParams.model_validate(params)
        pattern = re.compile(args.regex)
        root = resolve_host_path(ctx.cwd, args.path)
        files = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        matches: list[str] = []
        for path in sorted(files):
            resolved = path.resolve()
            if root.is_dir() and not resolved.is_relative_to(root):
                continue
            rel = display_path(ctx.cwd, resolved)
            scoped = resolved.relative_to(root) if root.is_dir() else Path(resolved.name)
            if not args.include_hidden and any(part.startswith(".") for part in scoped.parts):
                continue
            for line_number, line in enumerate(
                resolved.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if pattern.search(line):
                    matches.append(f"{rel}:{line_number}: {line}")
                    if len(matches) >= args.max_results:
                        return ToolResult(content="\n".join(matches))
        return ToolResult(content="\n".join(matches) or "[no matches]")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_scope(
    ctx: ToolContext,
    *,
    params_equal: dict[str, object],
    label: str,
    path: Path,
    directory: Path,
) -> GrantScope:
    external = not is_workspace_path(ctx.cwd, path)
    return GrantScope(
        params_equal=params_equal,
        label=label,
        requires_permission=external,
        directory_param="path" if external else None,
        directory_path=str(directory) if external else None,
        directory_label=(f"{label.split(' at ', 1)[0]} under {directory}" if external else None),
    )
