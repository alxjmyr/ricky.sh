"""Profile-scoped persistent memory store."""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ricky.config import RickySettings, profile_data_path, user_data_path
from ricky.memory.markdown import parse_note, serialize_note
from ricky.memory.types import (
    MemoryCatalogEntry,
    MemoryLoadError,
    MemoryNote,
    MemoryNoteInput,
    NoteType,
)
from ricky.profiles import (
    SHARED_PROFILE,
    ProfileName,
    ProfileScope,
    validate_profile_name,
)

_INDEX_FILENAME = "INDEX.md"
_LOCK_FILENAME = ".lock"
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


class MemoryStore:
    """Read and write memory owned by profiles in one fixed runtime scope."""

    def __init__(
        self,
        *,
        scope: ProfileScope,
        profile_dirs: dict[ProfileName, Path],
        settings: RickySettings,
    ) -> None:
        self.scope = scope
        self.index_char_limit = settings.memory.index_char_limit
        self.recall_char_limit = settings.memory.recall_char_limit
        self.recall_note_limit = settings.memory.recall_note_limit
        self.note_body_char_limit = settings.memory.note_body_char_limit
        self._profiles = scope.profiles
        self._profile_dirs = profile_dirs
        self._notes: dict[tuple[ProfileName, str], MemoryNote] = {}
        self._errors: list[MemoryLoadError] = []

    @classmethod
    def create(cls, settings: RickySettings, *, scope: ProfileScope) -> MemoryStore:
        """Create and scan only the memory roots issued to this runtime."""

        user_root = user_data_path(settings)
        _ensure_private_directory(user_root)
        _ensure_private_directory(user_root / "profiles")
        profile_dirs: dict[ProfileName, Path] = {}
        for profile in scope.profiles:
            unresolved_profile_root = user_root / "profiles" / profile
            if unresolved_profile_root.is_symlink():
                raise ValueError(
                    f"memory profile directory must not be a symlink: {unresolved_profile_root}"
                )
            profile_root = profile_data_path(settings, profile)
            _ensure_private_directory(profile_root)
            memory_root = profile_root / "memory"
            _ensure_private_directory(memory_root)
            profile_dirs[profile] = memory_root

        store = cls(scope=scope, profile_dirs=profile_dirs, settings=settings)
        for profile in scope.profiles:
            with store._locked_profiles((profile,)):
                store._refresh_profile(profile)
                store._write_index_safely(profile)
        return store

    def accessible_profiles(self) -> list[ProfileName]:
        """Return the profiles this store is allowed to access."""

        return list(self._profiles)

    def default_write_profile(self) -> ProfileName:
        """Return the session's primary profile for unqualified writes."""

        return self.scope.primary

    def require_profile(self, profile: str) -> ProfileName:
        """Validate that one profile belongs to this store's immutable scope."""

        name = validate_profile_name(profile)
        if name not in self._profiles:
            accessible = ", ".join(self._profiles)
            raise ValueError(
                f"profile {name!r} is not accessible in this memory scope; "
                f"accessible profiles: {accessible}"
            )
        return name

    def root_for(self, profile: str) -> Path:
        """Return one accessible profile's private memory root."""

        accessible = self.require_profile(profile)
        return self._profile_dirs[accessible]

    def catalog(self) -> list[MemoryCatalogEntry]:
        entries = [MemoryCatalogEntry.from_note(note) for note in self._notes.values()]
        return sorted(entries, key=lambda item: (item.type, item.slug, item.profile))

    def errors(self) -> list[MemoryLoadError]:
        return list(self._errors)

    def get(self, *, profile: str, slug: str) -> MemoryNote | None:
        accessible = self.require_profile(profile)
        return self._notes.get((accessible, slug))

    def matches_for_slug(self, slug: str) -> list[MemoryNote]:
        return sorted(
            (note for note in self._notes.values() if note.slug == slug),
            key=lambda note: note.profile,
        )

    def render_index(self, char_limit: int | None = None) -> str | None:
        """Render the compact accessible-profile catalog within a hard ceiling."""

        entries = self.catalog()
        if not entries:
            return None
        limit = char_limit if char_limit is not None else self.index_char_limit
        if limit < 1:
            raise ValueError("memory index character limit must be positive")

        for included in range(len(entries), -1, -1):
            lines = self._index_lines(entries[:included])
            if included < len(entries):
                dropped = len(entries) - included
                noun = "entry" if dropped == 1 else "entries"
                lines.append(f"[{dropped} catalog {noun} omitted; use recall.]")
            rendered = "\n".join(lines)
            if len(rendered) <= limit:
                return rendered

        marker = f"[{len(entries)} catalog entries omitted; use recall.]"
        return marker[:limit]

    def search(
        self,
        *,
        query: str | None = None,
        slugs: list[str] | None = None,
        profiles: Sequence[str] | None = None,
        note_type: NoteType | None = None,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[MemoryNote]:
        """Search notes in one, many, or all profiles available to this runtime."""

        if limit < 1:
            raise ValueError("memory search limit must be positive")
        selected_profiles = (
            set(self._profiles)
            if profiles is None
            else {self.require_profile(profile) for profile in profiles}
        )

        slug_filter = set(slugs) if slugs is not None else None
        tag_filter = {tag.casefold() for tag in tags or []}
        needle = query.casefold() if query else None
        matches: list[MemoryNote] = []
        for note in self._notes.values():
            if note.profile not in selected_profiles:
                continue
            if note_type is not None and note.type != note_type:
                continue
            if slug_filter is not None and note.slug not in slug_filter:
                continue
            note_tags = {tag.casefold() for tag in note.tags}
            if tag_filter and not tag_filter.issubset(note_tags):
                continue
            if needle is not None and needle not in _search_text(note):
                continue
            matches.append(note)
        matches.sort(key=lambda note: note.updated_at, reverse=True)
        return matches[:limit]

    def write(self, note_input: MemoryNoteInput) -> MemoryNote:
        """Atomically create or fully replace one profile-qualified note."""

        profile = self.require_profile(note_input.profile)
        if len(note_input.body) > self.note_body_char_limit:
            raise ValueError(
                "memory note body exceeds configured limit: "
                f"{len(note_input.body)} > {self.note_body_char_limit}"
            )

        path = self._note_path(profile, note_input.slug)
        expected = self._notes.get((profile, note_input.slug))
        with self._locked_profiles((profile,)):
            self._refresh_profile(profile)
            existing = self._notes.get((profile, note_input.slug))
            if existing is None and path.exists():
                raise ValueError(f"refusing to overwrite unloaded or malformed memory note: {path}")
            if not _same_revision(expected, existing):
                raise _concurrent_change_error(profile, note_input.slug)

            now = datetime.now(UTC)
            if existing is not None and now <= existing.updated_at:
                now = existing.updated_at + timedelta(microseconds=1)
            created_at = existing.created_at if existing is not None else now
            note = MemoryNote(
                **note_input.model_dump(),
                created_at=created_at,
                updated_at=now,
            )
            _atomic_write(path, serialize_note(note))
            self._notes[(profile, note.slug)] = note
            self._write_index_safely(profile)
            return note

    def delete(self, slug: str, *, profile: str | None = None) -> bool:
        """Delete one unambiguous note from the accessible profile set."""

        if profile is not None:
            selected = self.require_profile(profile)
            profiles: tuple[ProfileName, ...] = (selected,)
            expected = _notes_for_slug(self._notes, slug, profile=selected)
        else:
            profiles = self._profiles
            expected = _notes_for_slug(self._notes, slug)

        with self._locked_profiles(profiles):
            for accessible_profile in profiles:
                self._refresh_profile(accessible_profile)
            matches = _notes_for_slug(self._notes, slug, profile=profile)
            if not _same_revisions(expected, matches):
                if not matches:
                    return False
                raise ValueError(
                    f"memory note changed in another session; review and retry: {slug}"
                )
            if len(matches) > 1:
                ids = ", ".join(f"{note.profile}/{note.slug}" for note in matches)
                raise ValueError(f"memory slug {slug!r} is ambiguous; specify profile: {ids}")

            if not matches:
                return False
            note = matches[0]
            path = self._note_path(note.profile, note.slug)
            path.unlink()
            self._notes.pop((note.profile, note.slug), None)
            self._write_index_safely(note.profile)
            return True

    def _index_lines(self, entries: Sequence[MemoryCatalogEntry]) -> list[str]:
        lines = [
            "Memory index (catalog only; note bodies are not loaded).",
            "Call recall before relying on a note. Qualified ids are profile/slug.",
            f"Accessible profiles: {', '.join(self._profiles)}.",
            "",
        ]
        current_type: NoteType | None = None
        for entry in entries:
            if entry.type != current_type:
                lines.append(f"## {entry.type}")
                current_type = entry.type
            lines.append(_catalog_line(entry))
        return lines

    def _refresh_profile(self, profile: ProfileName) -> None:
        profile_dir = self._profile_dirs[profile]
        profile_root = profile_dir.resolve()
        self._notes = {
            identity: note for identity, note in self._notes.items() if identity[0] != profile
        }
        self._errors = [
            error for error in self._errors if Path(error.source_path).parent != profile_dir
        ]
        for path in sorted(profile_dir.glob("*.md")):
            if path.name == _INDEX_FILENAME:
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(profile_root):
                    raise ValueError("memory note path escapes its profile directory")
                note = parse_note(path.read_text(encoding="utf-8"), profile=profile)
                if path.stem != note.slug:
                    raise ValueError(
                        f"memory filename {path.name!r} does not match slug {note.slug!r}"
                    )
                self._notes[(profile, note.slug)] = note
            except (OSError, ValueError) as exc:
                self._record_error(path, exc)

    def _note_path(self, profile: ProfileName, slug: str) -> Path:
        profile_dir = self._profile_dirs[profile]
        root = profile_dir.resolve()
        path = (profile_dir / f"{slug}.md").resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"memory slug escapes profile directory: {slug}")
        return path

    def _write_index(self, profile: ProfileName) -> None:
        entries = [entry for entry in self.catalog() if entry.profile == profile]
        lines = [
            "# Memory Index",
            "",
            "Generated from note frontmatter. Do not edit this file by hand.",
        ]
        current_type: NoteType | None = None
        for entry in entries:
            if entry.type != current_type:
                lines.extend(["", f"## {entry.type}"])
                current_type = entry.type
            lines.append(_catalog_line(entry))
        lines.append("")
        _atomic_write(self._profile_dirs[profile] / _INDEX_FILENAME, "\n".join(lines))

    def _write_index_safely(self, profile: ProfileName) -> None:
        path = self._profile_dirs[profile] / _INDEX_FILENAME
        try:
            self._write_index(profile)
        except OSError as exc:
            self._record_error(path, exc)

    def _record_error(self, path: Path, exc: OSError | ValueError) -> None:
        self._errors = [error for error in self._errors if error.source_path != str(path)]
        self._errors.append(MemoryLoadError(source_path=str(path), message=str(exc)))

    @contextmanager
    def _locked_profiles(self, profiles: Sequence[ProfileName]) -> Iterator[None]:
        ordered = sorted(set(profiles), key=lambda item: (item != SHARED_PROFILE, item))
        with ExitStack() as stack:
            for profile in ordered:
                stack.enter_context(
                    _exclusive_file_lock(self._profile_dirs[profile] / _LOCK_FILENAME)
                )
            yield


def memory_note_counts(
    settings: RickySettings,
    *,
    scope: ProfileScope | None = None,
) -> dict[str, int]:
    """Count note files by profile without creating or changing anything."""

    profiles = scope.profiles if scope is not None else tuple(settings.profiles.enabled)
    counts: dict[str, int] = {}
    for profile in profiles:
        profile_dir = profile_data_path(settings, profile) / "memory"
        if not profile_dir.is_dir():
            counts[profile] = 0
            continue
        counts[profile] = sum(
            1
            for path in profile_dir.iterdir()
            if path.is_file() and path.suffix == ".md" and path.name != _INDEX_FILENAME
        )
    return counts


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"memory directory must not be a symlink: {path}")
    path.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    path.chmod(_DIRECTORY_MODE)


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, _FILE_MODE)
    try:
        os.fchmod(descriptor, _FILE_MODE)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, _FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _catalog_line(entry: MemoryCatalogEntry) -> str:
    details: list[str] = [entry.qualified_id, entry.title, entry.summary]
    if entry.tags:
        details.append(f"tags: {', '.join(entry.tags)}")
    if entry.related:
        details.append(f"related: {', '.join(entry.related)}")
    return f"- {' | '.join(details)}"


def _search_text(note: MemoryNote) -> str:
    return "\n".join([note.slug, note.title, note.summary, *note.tags, note.body]).casefold()


def _notes_for_slug(
    notes: dict[tuple[ProfileName, str], MemoryNote],
    slug: str,
    *,
    profile: str | None = None,
) -> list[MemoryNote]:
    return sorted(
        (
            note
            for note in notes.values()
            if note.slug == slug and (profile is None or note.profile == profile)
        ),
        key=lambda note: note.profile,
    )


def _same_revision(expected: MemoryNote | None, actual: MemoryNote | None) -> bool:
    if expected is None or actual is None:
        return expected is actual
    return (
        expected.profile == actual.profile
        and expected.slug == actual.slug
        and expected.created_at == actual.created_at
        and expected.updated_at == actual.updated_at
    )


def _same_revisions(expected: Sequence[MemoryNote], actual: Sequence[MemoryNote]) -> bool:
    return len(expected) == len(actual) and all(
        _same_revision(expected_note, actual_note)
        for expected_note, actual_note in zip(expected, actual, strict=True)
    )


def _concurrent_change_error(profile: ProfileName, slug: str) -> ValueError:
    return ValueError(f"memory note changed in another session; recall and retry: {profile}/{slug}")
