"""TOML-frontmatter Markdown codec for memory notes."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime

import tomlkit
from pydantic import ValidationError

from ricky.memory.types import MemoryNote
from ricky.profiles import ProfileName


def parse_note(text: str, *, profile: ProfileName) -> MemoryNote:
    """Parse and validate one memory note from Markdown text."""

    frontmatter, body = _split_frontmatter(text)
    try:
        raw = tomllib.loads(frontmatter)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid memory note TOML frontmatter: {exc}") from exc

    declared_profile = raw.get("profile")
    if declared_profile != profile:
        raise ValueError(
            "memory note profile mismatch: "
            f"file is in {profile!r}, frontmatter says {declared_profile!r}"
        )

    try:
        return MemoryNote.model_validate({**raw, "body": body})
    except ValidationError as exc:
        raise ValueError(f"invalid memory note metadata: {exc}") from exc


def serialize_note(note: MemoryNote) -> str:
    """Serialize one memory note to canonical TOML-frontmatter Markdown."""

    document = tomlkit.document()
    document["slug"] = note.slug
    document["title"] = note.title
    document["type"] = note.type
    document["profile"] = note.profile
    document["summary"] = note.summary
    document["tags"] = list(note.tags)
    document["related"] = list(note.related)
    if note.source is not None:
        document["source"] = note.source
    document["created_at"] = _utc_text(note.created_at)
    document["updated_at"] = _utc_text(note.updated_at)
    return f"---\n{tomlkit.dumps(document)}---\n\n{note.body}"


def _split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("memory note must start with frontmatter delimiter '---'")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            frontmatter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            if body.startswith("\n"):
                body = body[1:]
            return frontmatter, body
    raise ValueError("memory note is missing closing frontmatter delimiter '---'")


def _utc_text(value: datetime) -> str:
    timestamp = value.astimezone(UTC)
    return timestamp.isoformat().replace("+00:00", "Z")
