"""Model-facing persistent memory tools."""

from __future__ import annotations

import difflib
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field

from ricky.memory.store import MemoryStore
from ricky.memory.types import (
    MemoryNote,
    MemoryNoteInput,
    MemorySlug,
    NoteType,
    slugify,
)
from ricky.permissions import GrantScope
from ricky.profiles import ProfileName
from ricky.tools.base import Risk, Tool, ToolContext, ToolResult

_PERMISSION_PREVIEW_LIMIT = 12_000


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RecallParams(_Params):
    query: str | None = None
    slugs: list[MemorySlug] | None = None
    profiles: list[ProfileName] | None = None
    type: NoteType | None = None
    tags: list[str] | None = None
    limit: int = Field(default=5, ge=1, le=50)


class RememberParams(_Params):
    slug: MemorySlug | None = None
    title: str = Field(min_length=1)
    type: NoteType = "topic"
    summary: str = Field(min_length=1)
    body: str
    profile: ProfileName | None = None
    tags: list[str] = Field(default_factory=list)
    related: list[MemorySlug] = Field(default_factory=list)
    source: str | None = None


class ForgetParams(_Params):
    slug: MemorySlug
    profile: ProfileName | None = None


class RecallTool:
    name: ClassVar[str] = "recall"
    description: ClassVar[str] = (
        "Read full persistent memory notes by keyword, selected accessible profiles, "
        "type, tags, or slug. Recall a note before relying on it or replacing it."
    )
    Params: ClassVar[type[BaseModel]] = RecallParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.memory.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = RecallParams.model_validate(params)
        if args.limit > self._store.recall_note_limit:
            return ToolResult(
                content=(
                    "recall limit exceeds configured maximum: "
                    f"{args.limit} > {self._store.recall_note_limit}"
                ),
                is_error=True,
            )
        try:
            notes = self._store.search(
                query=args.query,
                slugs=list(args.slugs) if args.slugs is not None else None,
                profiles=args.profiles,
                note_type=args.type,
                tags=args.tags,
                limit=args.limit,
            )
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)
        if not notes:
            return ToolResult(content="[no matching notes]")
        rendered = "\n\n".join(_render_note(note) for note in notes)
        return ToolResult(content=_truncate(rendered, self._store.recall_char_limit))


class RememberTool:
    name: ClassVar[str] = "remember"
    description: ClassVar[str] = (
        "Create or fully replace one persistent memory note. Recall an existing note "
        "first and send the complete merged body. Omit profile to use the session's "
        "primary profile."
    )
    Params: ClassVar[type[BaseModel]] = RememberParams
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.memory.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        del ctx
        normalized = dict(args)
        if normalized.get("profile") is None:
            normalized["profile"] = self._store.default_write_profile()
        if normalized.get("slug") is None and isinstance(normalized.get("title"), str):
            normalized["slug"] = slugify(cast(str, normalized["title"]))
        return normalized

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        del ctx
        profile = str(args.get("profile", self._store.default_write_profile()))
        return GrantScope(
            params_equal={"profile": profile},
            label=f"remember to {profile} memory",
            allow_unconstrained=False,
        )

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        note_input = self._note_input(RememberParams.model_validate(args))
        try:
            existing = self._store.get(profile=note_input.profile, slug=note_input.slug)
        except ValueError as exc:
            return f"cannot write memory note: {exc}"
        action = "update" if existing is not None else "create"
        header = [
            f"{action} memory note: {note_input.profile}/{note_input.slug}",
            f"title: {note_input.title}",
            f"type: {note_input.type}",
            f"summary: {note_input.summary}",
            f"tags: {', '.join(note_input.tags) if note_input.tags else '(none)'}",
            f"related: {', '.join(note_input.related) if note_input.related else '(none)'}",
            f"source: {note_input.source or '(none)'}",
        ]
        if existing is None:
            preview = "\n".join([*header, "", "Proposed body:", note_input.body])
        else:
            metadata = _metadata_changes(existing, note_input)
            body_diff = "\n".join(
                difflib.unified_diff(
                    existing.body.splitlines(),
                    note_input.body.splitlines(),
                    fromfile=f"{existing.profile}/{existing.slug} before",
                    tofile=f"{note_input.profile}/{note_input.slug} after",
                    lineterm="",
                )
            )
            preview = "\n".join(
                [
                    *header,
                    "",
                    "Metadata changes:",
                    metadata or "(none)",
                    "",
                    "Body diff:",
                    body_diff or "(none)",
                ]
            )
        return _truncate(preview, _PERMISSION_PREVIEW_LIMIT)

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = RememberParams.model_validate(params)
        note_input = self._note_input(args)
        try:
            existed = self._store.get(profile=note_input.profile, slug=note_input.slug) is not None
            note = self._store.write(note_input)
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)
        action = "updated" if existed else "created"
        return ToolResult(content=f"Memory note {action}: {note.profile}/{note.slug}")

    def _note_input(self, args: RememberParams) -> MemoryNoteInput:
        return MemoryNoteInput(
            slug=args.slug or slugify(args.title),
            title=args.title,
            type=args.type,
            profile=args.profile or self._store.default_write_profile(),
            summary=args.summary,
            tags=args.tags,
            related=args.related,
            source=args.source,
            body=args.body,
        )


class ForgetTool:
    name: ClassVar[str] = "forget"
    description: ClassVar[str] = (
        "Permanently delete one persistent memory note. Specify profile when a slug "
        "exists in more than one accessible profile."
    )
    Params: ClassVar[type[BaseModel]] = ForgetParams
    risk: ClassVar[Risk] = "destructive"
    capability_id = "builtin.memory.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        params = ForgetParams.model_validate(args)
        try:
            matches = (
                [self._store.get(profile=params.profile, slug=params.slug)]
                if params.profile is not None
                else self._store.matches_for_slug(params.slug)
            )
        except ValueError as exc:
            return f"cannot delete memory note: {exc}"
        notes = [note for note in matches if note is not None]
        if not notes:
            return f"delete memory note: {params.profile or '*'}/{params.slug} (not found)"
        if len(notes) > 1:
            ids = ", ".join(f"{note.profile}/{note.slug}" for note in notes)
            return f"ambiguous memory delete; specify profile: {ids}"
        note = notes[0]
        return _truncate(
            "\n".join(
                [
                    f"permanently delete memory note: {note.profile}/{note.slug}",
                    f"title: {note.title}",
                    f"summary: {note.summary}",
                    "",
                    note.body,
                ]
            ),
            _PERMISSION_PREVIEW_LIMIT,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = ForgetParams.model_validate(params)
        selected_profile = args.profile
        if selected_profile is None:
            matches = self._store.matches_for_slug(args.slug)
            if len(matches) == 1:
                selected_profile = matches[0].profile
        try:
            deleted = self._store.delete(args.slug, profile=args.profile)
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)
        if not deleted:
            return ToolResult(content="Memory note not found.")
        identity = f"{selected_profile}/{args.slug}"
        return ToolResult(content=f"Memory note deleted: {identity}")


def memory_tools(store: MemoryStore) -> list[Tool]:
    """Build the three tools that share one profile-scoped store."""

    return [RecallTool(store), RememberTool(store), ForgetTool(store)]


def _render_note(note: MemoryNote) -> str:
    return "\n".join(
        [
            f"# {note.title}",
            f"id: {note.profile}/{note.slug}",
            f"type: {note.type}",
            f"summary: {note.summary}",
            f"tags: {', '.join(note.tags) if note.tags else '(none)'}",
            f"related: {', '.join(note.related) if note.related else '(none)'}",
            f"source: {note.source or '(none)'}",
            f"created_at: {note.created_at.isoformat()}",
            f"updated_at: {note.updated_at.isoformat()}",
            "",
            note.body,
        ]
    )


def _metadata_changes(existing: MemoryNote, proposed: MemoryNoteInput) -> str:
    lines: list[str] = []
    for field in ("title", "type", "summary", "tags", "related", "source"):
        before = getattr(existing, field)
        after = getattr(proposed, field)
        if before != after:
            lines.extend([f"- {field}: {before}", f"+ {field}: {after}"])
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[truncated]"
    return f"{text[: max(0, limit - len(marker))]}{marker}"
