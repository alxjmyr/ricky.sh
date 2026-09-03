"""Outbound attachment host-boundary and durable snapshot tests."""

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from ricky.attachments import (
    AttachmentInput,
    BrowserDownloadRef,
    PreparedAttachmentEffect,
    browser_download_path,
    delete_attachment_snapshots,
    load_attachments,
    read_stored_attachment,
    snapshot_attachment_batch,
    snapshot_attachments,
)
from ricky.config import GmailSettings, RickySettings, SlackSettings
from ricky.profiles import ProfileScope
from ricky.tools import EffectIdentity

_PERSONAL_SCOPE = ProfileScope.create("personal")


def _browser_download(content: bytes = b"download") -> BrowserDownloadRef:
    return BrowserDownloadRef(
        id="browser_download_" + "a" * 32,
        profile="personal",
        filename="report.txt",
        media_type="text/plain",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_logical_browser_download_is_scope_and_digest_bound(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project-data"),
    )
    reference = _browser_download()
    path = browser_download_path(settings, reference)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"download")

    [loaded] = load_attachments(
        [AttachmentInput(browser_download=reference)],
        cwd=project,
        settings=settings,
        profile_scope=_PERSONAL_SCOPE,
        count_limit=1,
        file_byte_limit=100,
        total_byte_limit=100,
    )

    assert loaded.content == b"download"
    assert loaded.filename == "report.txt"
    assert loaded.source_path == path
    assert not Path(settings.project_data_dir).exists()

    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="size|digest"):
        load_attachments(
            [AttachmentInput(browser_download=reference)],
            cwd=project,
            settings=settings,
            profile_scope=_PERSONAL_SCOPE,
            count_limit=1,
            file_byte_limit=100,
            total_byte_limit=100,
        )


def test_browser_download_rejects_wrong_scope_invention_symlink_and_mixed_source(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    work = _browser_download().model_copy(update={"profile": "work"})
    with pytest.raises(ValueError, match="outside the active profile scope"):
        load_attachments(
            [AttachmentInput(browser_download=work)],
            cwd=project,
            settings=settings,
            profile_scope=_PERSONAL_SCOPE,
            count_limit=1,
            file_byte_limit=100,
            total_byte_limit=100,
        )

    missing = _browser_download()
    with pytest.raises(ValueError, match="not a regular file"):
        load_attachments(
            [AttachmentInput(browser_download=missing)],
            cwd=project,
            settings=settings,
            profile_scope=_PERSONAL_SCOPE,
            count_limit=1,
            file_byte_limit=100,
            total_byte_limit=100,
        )

    destination = browser_download_path(settings, missing)
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"download")
    destination.symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe|not a regular file"):
        load_attachments(
            [AttachmentInput(browser_download=missing)],
            cwd=project,
            settings=settings,
            profile_scope=_PERSONAL_SCOPE,
            count_limit=1,
            file_byte_limit=100,
            total_byte_limit=100,
        )

    with pytest.raises(ValidationError, match="exactly one"):
        AttachmentInput(path="report.txt", browser_download=missing)
    with pytest.raises(ValidationError, match="plain filename"):
        BrowserDownloadRef.model_validate(
            {**missing.model_dump(mode="python"), "filename": "../../escape"}
        )


def test_loads_project_and_external_host_files_but_rejects_private_state(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    user = tmp_path / "user"
    project_state = tmp_path / "project-state"
    outside = tmp_path / "outside.txt"
    project.mkdir()
    user.mkdir()
    project_state.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    (project / "project.txt").write_text("project")
    outside.write_text("outside")
    token = user / "google" / "tokens.json"
    token.parent.mkdir()
    token.write_text("token")
    database = user / "notifications.sqlite3"
    database.write_text("database")
    project_database = project_state / "sessions.sqlite3"
    project_database.write_text("database")
    secret = user / ".secrets.toml"
    secret.write_text('probe_secret = "secret"\n')
    config = user / "ricky.toml"
    config.write_text('probe_config = "config"\n')
    private_link = project / "private-link"
    private_link.symlink_to(token)
    settings = RickySettings(
        user_data_dir=str(user),
        project_data_dir=str(project_state),
    )

    loaded = load_attachments(
        [
            AttachmentInput(path="project.txt"),
            AttachmentInput(path=str(outside), filename="renamed.txt"),
        ],
        cwd=project,
        settings=settings,
        profile_scope=_PERSONAL_SCOPE,
        count_limit=2,
        file_byte_limit=100,
        total_byte_limit=100,
    )

    assert [(item.filename, item.content) for item in loaded] == [
        ("project.txt", b"project"),
        ("renamed.txt", b"outside"),
    ]
    allowed_private_paths: list[Path] = []
    for private in (token, database, project_database, secret, config, private_link):
        try:
            load_attachments(
                [AttachmentInput(path=str(private))],
                cwd=project,
                settings=settings,
                profile_scope=_PERSONAL_SCOPE,
                count_limit=2,
                file_byte_limit=100,
                total_byte_limit=100,
            )
        except ValueError as exc:
            assert "private state or configuration" in str(exc)
        else:
            allowed_private_paths.append(private)
    assert allowed_private_paths == []


def test_designated_download_exports_remain_attachable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    gmail_export = user / "exports" / "gmail" / "message.pdf"
    slack_export = user / "exports" / "slack" / "upload.txt"
    gmail_export.parent.mkdir(parents=True)
    slack_export.parent.mkdir(parents=True)
    gmail_export.write_bytes(b"gmail")
    slack_export.write_bytes(b"slack")
    settings = RickySettings(
        user_data_dir=str(user),
        gmail=GmailSettings(download_dir="exports/gmail"),
        slack=SlackSettings(download_dir="exports/slack"),
    )

    loaded = load_attachments(
        [AttachmentInput(path=str(gmail_export)), AttachmentInput(path=str(slack_export))],
        cwd=project,
        settings=settings,
        profile_scope=_PERSONAL_SCOPE,
        count_limit=2,
        file_byte_limit=100,
        total_byte_limit=100,
    )

    assert [item.content for item in loaded] == [b"gmail", b"slack"]


def test_profile_gmail_download_requires_owning_profile_in_scope(tmp_path: Path) -> None:
    project = tmp_path / "project"
    user = tmp_path / "user"
    project_data = tmp_path / "project-data"
    project.mkdir()
    download = user / "profiles" / "work" / "downloads" / "gmail" / "message.pdf"
    download.parent.mkdir(parents=True)
    download.write_bytes(b"profile gmail")
    settings = RickySettings(
        user_data_dir=str(user),
        project_data_dir=str(project_data),
    )

    loaded = load_attachments(
        [AttachmentInput(path=str(download))],
        cwd=project,
        settings=settings,
        profile_scope=ProfileScope.create("personal", access_profiles=["work"]),
        count_limit=1,
        file_byte_limit=100,
        total_byte_limit=100,
    )

    assert loaded[0].content == b"profile gmail"
    with pytest.raises(ValueError, match="private state or configuration"):
        load_attachments(
            [AttachmentInput(path=str(download))],
            cwd=project,
            settings=settings,
            profile_scope=_PERSONAL_SCOPE,
            count_limit=1,
            file_byte_limit=100,
            total_byte_limit=100,
        )
    assert not project_data.exists()


def test_snapshots_only_below_user_data_and_digest_checks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    source = project / "report.pdf"
    source.write_bytes(b"pdf-data")
    settings = RickySettings(user_data_dir=str(user))
    loaded = load_attachments(
        [AttachmentInput(path="report.pdf", media_type="application/pdf")],
        cwd=project,
        settings=settings,
        profile_scope=_PERSONAL_SCOPE,
        count_limit=1,
        file_byte_limit=100,
        total_byte_limit=100,
    )

    stored = snapshot_attachments(
        loaded,
        settings=settings,
        notification_id="notification_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )[0]

    assert read_stored_attachment(stored, user_root=user) == b"pdf-data"
    assert list(project.iterdir()) == [source]
    (user / stored.storage_path).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="size|digest"):
        read_stored_attachment(stored, user_root=user)


def test_prepared_attachment_payload_survives_json_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "binary.dat"
    source.write_bytes(b"\x00\xffprepared\x80bytes")
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    [attachment] = load_attachments(
        [AttachmentInput(path="binary.dat")],
        cwd=project,
        settings=settings,
        profile_scope=_PERSONAL_SCOPE,
        count_limit=1,
        file_byte_limit=100,
        total_byte_limit=100,
    )
    prepared = PreparedAttachmentEffect(
        tool_name="test_effect",
        identity=EffectIdentity(
            operation="test.effect",
            target="target",
            occurrence="one",
            summary="Test prepared bytes",
            action_key="a" * 64,
        ),
        permission_summary="Prepared exact bytes",
        attachments=(attachment,),
    )

    restored = PreparedAttachmentEffect.model_validate_json(prepared.model_dump_json())

    assert restored == prepared
    assert restored.attachments[0].content == b"\x00\xffprepared\x80bytes"


def test_partial_multi_file_snapshot_failure_cleans_created_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "first.txt").write_bytes(b"first")
    (project / "second.txt").write_bytes(b"second")
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    loaded = load_attachments(
        [AttachmentInput(path="first.txt"), AttachmentInput(path="second.txt")],
        cwd=project,
        settings=settings,
        profile_scope=_PERSONAL_SCOPE,
        count_limit=2,
        file_byte_limit=100,
        total_byte_limit=100,
    )
    import ricky.attachments as attachments_module

    original = attachments_module._write_atomic_once
    calls = 0

    def fail_second(path: Path, content: bytes) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second snapshot failure")
        return original(path, content)

    monkeypatch.setattr(attachments_module, "_write_atomic_once", fail_second)

    with pytest.raises(OSError, match="second snapshot failure"):
        snapshot_attachment_batch(
            loaded,
            settings=settings,
            notification_id="notification_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )

    attachment_root = Path(settings.user_data_dir) / settings.messaging.attachment_dir
    assert not attachment_root.exists() or not any(attachment_root.rglob("*"))


def test_snapshot_cleanup_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "report.txt").write_bytes(b"report")
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    loaded = load_attachments(
        [AttachmentInput(path="report.txt")],
        cwd=project,
        settings=settings,
        profile_scope=_PERSONAL_SCOPE,
        count_limit=1,
        file_byte_limit=100,
        total_byte_limit=100,
    )
    batch = snapshot_attachment_batch(
        loaded,
        settings=settings,
        notification_id="notification_cccccccccccccccccccccccccccccccc",
    )

    delete_attachment_snapshots(settings, batch.created_storage_paths)
    delete_attachment_snapshots(settings, batch.created_storage_paths)

    attachment_root = Path(settings.user_data_dir) / settings.messaging.attachment_dir
    assert not attachment_root.exists() or not any(attachment_root.rglob("*"))


def test_loads_logical_durable_task_artifact_without_exposing_storage_path(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    task_id = "task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    artifact = user / "profiles" / "personal" / "tasks" / "artifacts" / task_id / "map.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html>map</html>")
    settings = RickySettings(user_data_dir=str(user))

    loaded = load_attachments(
        [
            AttachmentInput(
                task_id=task_id,
                task_artifact_path="map.html",
                profile="personal",
            )
        ],
        cwd=project,
        settings=settings,
        profile_scope=_PERSONAL_SCOPE,
        count_limit=1,
        file_byte_limit=100,
        total_byte_limit=100,
    )

    assert loaded[0].filename == "map.html"
    assert loaded[0].content == b"<html>map</html>"


def test_rejects_logical_task_artifact_from_another_profile(tmp_path: Path) -> None:
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    task_id = "task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    artifact = user / "profiles" / "work" / "tasks" / "artifacts" / task_id / "map.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html>work</html>")
    settings = RickySettings(user_data_dir=str(user))

    with pytest.raises(ValueError, match="outside the active profile scope"):
        load_attachments(
            [
                AttachmentInput(
                    task_id=task_id,
                    task_artifact_path="map.html",
                    profile="work",
                )
            ],
            cwd=project,
            settings=settings,
            profile_scope=_PERSONAL_SCOPE,
            count_limit=1,
            file_byte_limit=100,
            total_byte_limit=100,
        )


def test_missing_local_attachment_recommends_logical_task_reference(tmp_path: Path) -> None:
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    settings = RickySettings(user_data_dir=str(user))

    with pytest.raises(ValueError, match="task_id, task_artifact_path, and profile"):
        load_attachments(
            [AttachmentInput(path="guessed/task/storage/map.html")],
            cwd=project,
            settings=settings,
            profile_scope=_PERSONAL_SCOPE,
            count_limit=1,
            file_byte_limit=100,
            total_byte_limit=100,
        )
