"""Persistent memory store safety and profile-scope behavior tests."""

from __future__ import annotations

import re
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ricky.config import MemorySettings, RickySettings
from ricky.memory import MemoryStore, memory_note_counts
from ricky.memory.types import MemoryNoteInput, NoteType
from ricky.profiles import ProfileName, ProfileScope


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "global"),
        project_data_dir=str(tmp_path / "project-data"),
        memory=MemorySettings(
            index_char_limit=1_000,
            recall_char_limit=1_000,
            recall_note_limit=10,
            note_body_char_limit=500,
        ),
    )


def _scope(
    primary: str = "personal",
    *,
    access_profiles: tuple[str, ...] = (),
) -> ProfileScope:
    return ProfileScope.create(primary, access_profiles=access_profiles)


def _input(
    *,
    slug: str,
    profile: ProfileName,
    body: str,
    title: str | None = None,
    summary: str | None = None,
    note_type: NoteType = "topic",
    tags: list[str] | None = None,
) -> MemoryNoteInput:
    return MemoryNoteInput(
        slug=slug,
        title=title or slug.replace("-", " ").title(),
        type=note_type,
        profile=profile,
        summary=summary or f"Summary for {slug}",
        tags=tags or [],
        body=body,
    )


def test_store_scans_only_issued_profiles_and_multi_profile_scope_reads_all(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    personal = MemoryStore.create(settings, scope=_scope())
    personal.write(_input(slug="shared-fact", profile="shared", body="shared body"))
    personal.write(_input(slug="personal-fact", profile="personal", body="personal body"))

    work = MemoryStore.create(settings, scope=_scope("work"))
    work.write(_input(slug="work-secret", profile="work", body="classified work body"))

    personal_again = MemoryStore.create(settings, scope=_scope())
    assert personal_again.accessible_profiles() == ["shared", "personal"]
    assert {entry.slug for entry in personal_again.catalog()} == {
        "shared-fact",
        "personal-fact",
    }
    assert personal_again.search(slugs=["work-secret"]) == []
    assert "classified work body" not in (personal_again.render_index() or "")

    work_again = MemoryStore.create(settings, scope=_scope("work"))
    assert work_again.accessible_profiles() == ["shared", "work"]
    assert {entry.slug for entry in work_again.catalog()} == {"shared-fact", "work-secret"}

    cross_profile = MemoryStore.create(
        settings,
        scope=_scope("work", access_profiles=("personal",)),
    )
    assert cross_profile.accessible_profiles() == ["shared", "personal", "work"]
    assert {entry.slug for entry in cross_profile.catalog()} == {
        "shared-fact",
        "personal-fact",
        "work-secret",
    }


def test_writes_default_to_primary_and_fail_outside_scope(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    personal = MemoryStore.create(settings, scope=_scope())
    work = MemoryStore.create(settings, scope=_scope("work"))

    assert personal.default_write_profile() == "personal"
    assert work.default_write_profile() == "work"

    with pytest.raises(ValueError, match="profile 'work' is not accessible"):
        personal.write(_input(slug="blocked", profile="work", body="no"))
    with pytest.raises(ValueError, match="profile 'personal' is not accessible"):
        work.write(_input(slug="blocked", profile="personal", body="no"))
    with pytest.raises(ValueError, match="profile 'work' is not accessible"):
        personal.search(profiles=["work"])


def test_update_preserves_created_at_and_fully_replaces_body(tmp_path: Path) -> None:
    store = MemoryStore.create(_settings(tmp_path), scope=_scope())
    first = store.write(
        _input(slug="preferences", profile="personal", body="Old fact", tags=["old"])
    )
    second = store.write(
        _input(slug="preferences", profile="personal", body="New fact", tags=["new"])
    )

    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at
    assert second.body == "New fact"
    assert second.tags == ["new"]
    note_text = (store.root_for("personal") / "preferences.md").read_text(encoding="utf-8")
    assert "Old fact" not in note_text
    assert 'profile = "personal"' in note_text


def test_profile_qualified_identity_and_ambiguous_delete(tmp_path: Path) -> None:
    store = MemoryStore.create(_settings(tmp_path), scope=_scope())
    store.write(_input(slug="alex", profile="shared", body="Shared Alex"))
    store.write(_input(slug="alex", profile="personal", body="Personal Alex"))

    matches = store.search(slugs=["alex"], limit=5)

    assert {(note.profile, note.body) for note in matches} == {
        ("shared", "Shared Alex"),
        ("personal", "Personal Alex"),
    }
    with pytest.raises(ValueError, match="ambiguous; specify profile"):
        store.delete("alex")
    assert store.delete("alex", profile="personal")
    assert store.get(profile="shared", slug="alex") is not None


def test_catalog_index_is_grouped_bounded_and_omits_bodies(tmp_path: Path) -> None:
    store = MemoryStore.create(_settings(tmp_path), scope=_scope())
    store.write(
        _input(
            slug="person-jane",
            profile="personal",
            title="Jane Doe",
            body="PRIVATE BODY",
            note_type="person",
        )
    )
    store.write(
        _input(
            slug="project-ricky",
            profile="shared",
            body="ANOTHER PRIVATE BODY",
            note_type="project",
        )
    )

    rendered = store.render_index(1_000)
    truncated = store.render_index(200)

    assert rendered is not None
    assert "## person" in rendered
    assert "## project" in rendered
    assert "personal/person-jane" in rendered
    assert "Accessible profiles: shared, personal." in rendered
    assert "PRIVATE BODY" not in rendered
    assert truncated is not None
    assert len(truncated) <= 200
    assert "omitted; use recall" in truncated


def test_search_filters_type_tags_profiles_and_query(tmp_path: Path) -> None:
    store = MemoryStore.create(_settings(tmp_path), scope=_scope())
    store.write(
        _input(
            slug="jane",
            profile="personal",
            body="Likes espresso",
            note_type="person",
            tags=["friend", "coffee"],
        )
    )
    store.write(
        _input(
            slug="acme",
            profile="shared",
            body="A public example",
            note_type="org",
            tags=["customer"],
        )
    )

    assert [note.slug for note in store.search(query="ESPRESSO")] == ["jane"]
    assert [note.slug for note in store.search(note_type="person")] == ["jane"]
    assert [note.slug for note in store.search(tags=["FRIEND", "coffee"])] == ["jane"]
    assert [note.slug for note in store.search(profiles=["shared"])] == ["acme"]
    assert {note.slug for note in store.search(profiles=["shared", "personal"])} == {
        "acme",
        "jane",
    }
    assert store.search(tags=["missing"]) == []


def test_files_and_indexes_are_private_and_malformed_notes_are_reported(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = MemoryStore.create(settings, scope=_scope())
    store.write(_input(slug="safe", profile="personal", body="safe"))
    profile_dir = store.root_for("personal")
    note_path = profile_dir / "safe.md"
    index_path = profile_dir / "INDEX.md"

    assert profile_dir == Path(settings.user_data_dir) / "profiles" / "personal" / "memory"
    assert stat.S_IMODE(profile_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(note_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(index_path.stat().st_mode) == 0o600

    (profile_dir / "broken.md").write_text("---\ninvalid\n---\n", encoding="utf-8")
    reloaded = MemoryStore.create(settings, scope=_scope())

    assert len(reloaded.errors()) == 1
    assert reloaded.errors()[0].source_path.endswith("broken.md")


def test_profile_memory_root_rejects_symlink_aliases(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles_root = Path(settings.user_data_dir) / "profiles"
    profiles_root.mkdir(parents=True)
    (profiles_root / "personal").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(ValueError, match="profile directory must not be a symlink"):
        MemoryStore.create(settings, scope=_scope())


def test_write_enforces_body_limit_and_refuses_malformed_overwrite(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = MemoryStore.create(settings, scope=_scope())

    with pytest.raises(ValueError, match="body exceeds"):
        store.write(_input(slug="too-long", profile="personal", body="x" * 501))

    malformed = store.root_for("personal") / "broken.md"
    malformed.write_text("---\ninvalid\n---\n", encoding="utf-8")
    reloaded = MemoryStore.create(settings, scope=_scope())
    with pytest.raises(ValueError, match="refusing to overwrite"):
        reloaded.write(_input(slug="broken", profile="personal", body="replacement"))


def test_concurrent_stores_serialize_profile_writes_and_refresh_the_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ricky.memory.store as store_module

    settings = _settings(tmp_path)
    first = MemoryStore.create(settings, scope=_scope())
    second = MemoryStore.create(settings, scope=_scope())
    first_in_write = threading.Event()
    release_first = threading.Event()
    second_in_write = threading.Event()
    original_atomic_write = store_module._atomic_write

    def controlled_atomic_write(path: Path, text: str) -> None:
        if path.name == "first.md":
            first_in_write.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("test did not release first memory write")
        elif path.name == "second.md":
            second_in_write.set()
        original_atomic_write(path, text)

    monkeypatch.setattr(store_module, "_atomic_write", controlled_atomic_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            first.write,
            _input(slug="first", profile="personal", body="first body"),
        )
        assert first_in_write.wait(timeout=5)
        second_future = executor.submit(
            second.write,
            _input(slug="second", profile="personal", body="second body"),
        )
        try:
            assert not second_in_write.wait(timeout=0.1)
        finally:
            release_first.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)

    index = (second.root_for("personal") / "INDEX.md").read_text(encoding="utf-8")
    assert "personal/first" in index
    assert "personal/second" in index
    assert second.get(profile="personal", slug="first") is not None


def test_stale_same_note_update_fails_instead_of_losing_newer_content(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    bootstrap = MemoryStore.create(settings, scope=_scope())
    bootstrap.write(_input(slug="preferences", profile="personal", body="original"))
    first = MemoryStore.create(settings, scope=_scope())
    stale = MemoryStore.create(settings, scope=_scope())

    first.write(_input(slug="preferences", profile="personal", body="newer content"))

    with pytest.raises(ValueError, match="changed in another session; recall and retry"):
        stale.write(_input(slug="preferences", profile="personal", body="stale content"))

    reloaded = MemoryStore.create(settings, scope=_scope())
    note = reloaded.get(profile="personal", slug="preferences")
    assert note is not None
    assert note.body == "newer content"


def test_stale_write_cannot_resurrect_note_deleted_by_another_session(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    bootstrap = MemoryStore.create(settings, scope=_scope())
    bootstrap.write(_input(slug="obsolete", profile="personal", body="old content"))
    deleter = MemoryStore.create(settings, scope=_scope())
    stale = MemoryStore.create(settings, scope=_scope())

    assert deleter.delete("obsolete", profile="personal")

    with pytest.raises(ValueError, match="changed in another session; recall and retry"):
        stale.write(_input(slug="obsolete", profile="personal", body="resurrected"))
    assert not (stale.root_for("personal") / "obsolete.md").exists()


def test_truncation_marker_counts_every_entry_removed_to_make_it_fit(
    tmp_path: Path,
) -> None:
    store = MemoryStore.create(_settings(tmp_path), scope=_scope())
    note_types: list[NoteType] = ["account", "org", "person", "project", "topic"]
    for index, note_type in enumerate(note_types):
        store.write(
            _input(
                slug=f"entry-{index}",
                profile="personal",
                body=f"body {index}",
                note_type=note_type,
                summary=f"A deliberately long summary for catalog entry {index}",
            )
        )

    full = store.render_index(10_000)
    assert full is not None
    entries = store.catalog()
    saw_marker = False
    for limit in range(40, len(full)):
        rendered = store.render_index(limit)
        assert rendered is not None
        match = re.search(r"\[(\d+) catalog entr(?:y|ies) omitted", rendered)
        if match is None:
            continue
        saw_marker = True
        visible = sum(entry.qualified_id in rendered for entry in entries)
        assert int(match.group(1)) == len(entries) - visible
        lines = rendered.splitlines()
        for line_index, line in enumerate(lines):
            if not line.startswith("## "):
                continue
            following_group = lines[line_index + 1 :]
            assert following_group and following_group[0].startswith("- ")
    assert saw_marker


def test_read_only_counts_do_not_create_roots_and_store_avoids_project_data(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    user_root = Path(settings.user_data_dir)
    project_root = Path(settings.project_data_dir)

    assert memory_note_counts(settings) == {"shared": 0, "personal": 0, "work": 0}
    assert not user_root.exists()
    assert not project_root.exists()

    store = MemoryStore.create(settings, scope=_scope())
    store.write(_input(slug="owned", profile="personal", body="private"))

    assert memory_note_counts(settings, scope=_scope()) == {"shared": 0, "personal": 1}
    assert (user_root / "profiles" / "personal" / "memory" / "owned.md").is_file()
    assert not project_root.exists()
