"""Canonical memory models."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ricky.profiles import ProfileName

NoteType = Literal["person", "org", "account", "project", "topic"]
MemorySlug = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
]


class _MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryNote(_MemoryModel):
    """One complete note loaded from or written to memory."""

    slug: MemorySlug
    title: str = Field(min_length=1)
    type: NoteType = "topic"
    profile: ProfileName
    summary: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    related: list[MemorySlug] = Field(default_factory=list)
    source: str | None = None
    created_at: datetime
    updated_at: datetime
    body: str

    @field_validator("summary")
    @classmethod
    def _summary_is_one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("summary must be one line")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timestamp_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("memory timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _timestamps_are_ordered(self) -> MemoryNote:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        return self


class MemoryNoteInput(_MemoryModel):
    """Caller-controlled note fields before store-owned timestamps."""

    slug: MemorySlug
    title: str = Field(min_length=1)
    type: NoteType = "topic"
    profile: ProfileName
    summary: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    related: list[MemorySlug] = Field(default_factory=list)
    source: str | None = None
    body: str

    @field_validator("summary")
    @classmethod
    def _summary_is_one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("summary must be one line")
        return value


class MemoryCatalogEntry(_MemoryModel):
    """Compact note metadata used by the always-loaded index."""

    slug: MemorySlug
    title: str
    type: NoteType
    profile: ProfileName
    summary: str
    tags: list[str] = Field(default_factory=list)
    related: list[MemorySlug] = Field(default_factory=list)
    updated_at: datetime

    @property
    def qualified_id(self) -> str:
        return f"{self.profile}/{self.slug}"

    @classmethod
    def from_note(cls, note: MemoryNote) -> MemoryCatalogEntry:
        return cls.model_validate(note.model_dump(exclude={"body", "source", "created_at"}))


class MemoryLoadError(_MemoryModel):
    """A malformed or unsafe note reported during store discovery."""

    source_path: str
    message: str


def slugify(title: str) -> str:
    """Derive a stable, bounded ASCII slug from a title."""

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64].rstrip("-")
    if slug:
        return slug
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
    return f"note-{digest}"
