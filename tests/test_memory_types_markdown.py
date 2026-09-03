"""Memory model and Markdown codec tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ricky.memory.markdown import parse_note, serialize_note
from ricky.memory.types import MemoryCatalogEntry, MemoryNote, slugify


def _note() -> MemoryNote:
    return MemoryNote(
        slug="org-acme",
        title="Acme Corp",
        type="org",
        profile="work",
        summary="Enterprise customer",
        tags=["customer", "renewal"],
        related=["person-jane-doe"],
        source="stated by Alex",
        created_at=datetime(2026, 7, 20, 14, tzinfo=UTC),
        updated_at=datetime(2026, 7, 22, 9, 30, tzinfo=UTC),
        body="Acme is an enterprise customer.\nPrimary contact is Jane.",
    )


def test_memory_models_survive_json_round_trip() -> None:
    note = _note()
    entry = MemoryCatalogEntry.from_note(note)

    assert MemoryNote.model_validate_json(note.model_dump_json()) == note
    assert MemoryCatalogEntry.model_validate_json(entry.model_dump_json()) == entry
    assert entry.qualified_id == "work/org-acme"
    assert "body" not in entry.model_dump()


def test_invalid_slug_and_multiline_summary_are_rejected() -> None:
    data = _note().model_dump()

    with pytest.raises(ValidationError, match="slug"):
        MemoryNote.model_validate({**data, "slug": "../escape"})
    with pytest.raises(ValidationError, match="summary must be one line"):
        MemoryNote.model_validate({**data, "summary": "line one\nline two"})


def test_parse_serialize_round_trip_preserves_lists_and_body() -> None:
    note = _note()

    text = serialize_note(note)
    restored = parse_note(text, profile="work")

    assert restored == note
    assert 'tags = ["customer", "renewal"]' in text
    assert 'related = ["person-jane-doe"]' in text
    assert 'created_at = "2026-07-20T14:00:00Z"' in text


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no frontmatter", "must start"),
        ('---\nslug = "x"', "missing closing"),
        ("---\nnot valid toml\n---\n", "TOML frontmatter"),
    ],
)
def test_malformed_note_has_clear_error(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_note(text, profile="personal")


def test_frontmatter_profile_must_match_directory_profile() -> None:
    with pytest.raises(ValueError, match="profile mismatch"):
        parse_note(serialize_note(_note()), profile="personal")


def test_slugify_has_stable_fallback_for_non_ascii_title() -> None:
    assert slugify("💡") == slugify("💡")
    assert slugify("💡").startswith("note-")
