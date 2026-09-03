"""Memory tool and permission-preview tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import ricky.memory.store as store_module
from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.memory import MemoryStore, memory_tools
from ricky.memory.types import MemoryNoteInput
from ricky.profiles import ProfileScope
from ricky.tools import ToolContext, ToolRegistry


def _setup(
    tmp_path: Path,
    *,
    primary: str = "personal",
    access_profiles: tuple[str, ...] = (),
):
    settings = RickySettings(user_data_dir=str(tmp_path / "global"))
    profile_scope = ProfileScope.create(primary, access_profiles=access_profiles)
    store = MemoryStore.create(settings, scope=profile_scope)
    registry = ToolRegistry(memory_tools(store))
    session = AgentSession.create(
        settings,
        profile_scope=profile_scope,
        provider="claude_code" if primary == "work" else "openrouter",
    )
    ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)
    return settings, store, registry, ctx


async def test_remember_normalizes_effective_scope_and_shows_reviewable_content(
    tmp_path: Path,
) -> None:
    _, store, registry, ctx = _setup(tmp_path)
    raw: dict[str, object] = {
        "title": "Answer Style",
        "summary": "Alex prefers terse answers",
        "body": "Prefer concise answers unless detail is requested.",
    }

    normalized = registry.permission_args("remember", raw, ctx)
    scope = registry.permission_scope("remember", normalized, ctx)
    create_preview = registry.permission_summary("remember", normalized, ctx)

    assert normalized["profile"] == "personal"
    assert normalized["slug"] == "answer-style"
    assert scope is not None
    assert scope.params_equal == {"profile": "personal"}
    assert scope.allow_unconstrained is False
    assert create_preview is not None
    assert "create memory note: personal/answer-style" in create_preview
    assert "Prefer concise answers" in create_preview

    created = await registry.dispatch("remember", raw, ctx)
    assert not created.is_error
    assert store.get(profile="personal", slug="answer-style") is not None

    updated_args: dict[str, object] = {
        **raw,
        "profile": "personal",
        "slug": "answer-style",
        "body": "Use short answers by default.",
    }
    update_preview = registry.permission_summary("remember", updated_args, ctx)

    assert update_preview is not None
    assert "update memory note" in update_preview
    assert "Body diff:" in update_preview
    assert "-Prefer concise answers unless detail is requested." in update_preview
    assert "+Use short answers by default." in update_preview

    updated = await registry.dispatch("remember", updated_args, ctx)
    assert not updated.is_error
    note = store.get(profile="personal", slug="answer-style")
    assert note is not None
    assert note.body == "Use short answers by default."


async def test_recall_filters_and_bounds_output(tmp_path: Path) -> None:
    settings, store, registry, ctx = _setup(tmp_path)
    store.write(
        MemoryNoteInput(
            slug="person-jane",
            title="Jane",
            type="person",
            profile="personal",
            summary="Friend who likes coffee",
            tags=["friend", "coffee"],
            body="Jane prefers espresso.",
        )
    )
    store.write(
        MemoryNoteInput(
            slug="org-acme",
            title="Acme",
            type="org",
            profile="shared",
            summary="Example org",
            tags=["customer"],
            body="Acme is an example.",
        )
    )

    result = await registry.dispatch(
        "recall",
        {"query": "ESPRESSO", "type": "person", "tags": ["coffee"]},
        ctx,
    )
    missing = await registry.dispatch("recall", {"slugs": ["missing"]}, ctx)
    shared_only = await registry.dispatch("recall", {"profiles": ["shared"]}, ctx)
    both_profiles = await registry.dispatch(
        "recall", {"profiles": ["shared", "personal"], "limit": 5}, ctx
    )
    inaccessible = await registry.dispatch("recall", {"profiles": ["work"]}, ctx)
    too_many = await registry.dispatch(
        "recall",
        {"limit": settings.memory.recall_note_limit + 1},
        ctx,
    )

    assert not result.is_error
    assert "id: personal/person-jane" in result.content
    assert "Jane prefers espresso" in result.content
    assert missing.content == "[no matching notes]"
    assert "shared/org-acme" in shared_only.content
    assert "personal/person-jane" not in shared_only.content
    assert "shared/org-acme" in both_profiles.content
    assert "personal/person-jane" in both_profiles.content
    assert inaccessible.is_error
    assert "profile 'work' is not accessible" in inaccessible.content
    assert too_many.is_error


async def test_remember_rejects_profile_outside_scope(tmp_path: Path) -> None:
    _, _, registry, ctx = _setup(tmp_path)

    result = await registry.dispatch(
        "remember",
        {
            "title": "Work Secret",
            "summary": "Must stay at work",
            "body": "secret",
            "profile": "work",
        },
        ctx,
    )

    assert result.is_error
    assert "profile 'work' is not accessible" in result.content


async def test_forget_is_destructive_and_requires_profile_when_ambiguous(
    tmp_path: Path,
) -> None:
    _, store, registry, ctx = _setup(tmp_path)
    for profile in ("shared", "personal"):
        store.write(
            MemoryNoteInput(
                slug="alex",
                title="Alex",
                profile=profile,
                summary=f"Alex in {profile}",
                body=f"{profile} body",
            )
        )

    forget = registry.get("forget")
    assert forget is not None
    assert forget.risk == "destructive"
    assert registry.permission_scope("forget", {"slug": "alex"}, ctx) is None

    preview = registry.permission_summary("forget", {"slug": "alex"}, ctx)
    ambiguous = await registry.dispatch("forget", {"slug": "alex"}, ctx)
    deleted = await registry.dispatch("forget", {"slug": "alex", "profile": "personal"}, ctx)

    assert preview is not None and "ambiguous" in preview
    assert ambiguous.is_error
    assert not deleted.is_error
    assert store.get(profile="personal", slug="alex") is None
    assert store.get(profile="shared", slug="alex") is not None


async def test_derived_index_failure_does_not_misreport_committed_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, registry, ctx = _setup(tmp_path)
    original_atomic_write = store_module._atomic_write

    def fail_index_write(path: Path, text: str) -> None:
        if path.name == "INDEX.md":
            raise OSError("index unavailable")
        original_atomic_write(path, text)

    monkeypatch.setattr(store_module, "_atomic_write", fail_index_write)
    created = await registry.dispatch(
        "remember",
        {
            "slug": "durable-note",
            "title": "Durable Note",
            "summary": "The note is the primary commit",
            "body": "Committed body",
            "profile": "personal",
        },
        ctx,
    )

    assert not created.is_error
    assert created.content == "Memory note created: personal/durable-note"
    assert (store.root_for("personal") / "durable-note.md").exists()
    assert any(
        error.source_path.endswith("INDEX.md") and "index unavailable" in error.message
        for error in store.errors()
    )

    deleted = await registry.dispatch(
        "forget",
        {"slug": "durable-note", "profile": "personal"},
        ctx,
    )
    assert not deleted.is_error
    assert deleted.content == "Memory note deleted: personal/durable-note"
    assert not (store.root_for("personal") / "durable-note.md").exists()
