"""Producer-neutral session media admission and bound egress tests."""

from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from PIL import Image

from ricky.agent import AgentSession
from ricky.agent.context import assemble_context
from ricky.config import RickySettings
from ricky.llm import ImagePart, Message, UserContent
from ricky.llm.openrouter import to_openrouter_request_with_media
from ricky.media import SessionMediaError, SessionMediaLimitError, SessionMediaStore
from ricky.profiles import ProfileLabel, ProfileScope
from ricky.tools import ToolRegistry


def _png(*, color: tuple[int, int, int] = (12, 34, 56), size: tuple[int, int] = (3, 2)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _settings(tmp_path, **updates: object) -> RickySettings:
    payload: dict[str, object] = {
        "user_data_dir": str(tmp_path / "user-data"),
        "project_data_dir": str(tmp_path / "project-data"),
    }
    payload.update(updates)
    return RickySettings.model_validate(payload)


async def test_synthetic_non_browser_image_uses_generic_store_content_and_resolver(
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    scope = settings.resolve_profile_scope()
    session = AgentSession.create(settings, profile_scope=scope, provider="openrouter")
    store = SessionMediaStore.create(settings, session.id)
    content = _png()

    assert not store.root.exists()
    record = await store.admit_png(
        session,
        content=content,
        source_label=ProfileLabel.owned_by(scope.primary),
        source_owner=scope.primary,
        provenance="synthetic_fixture",
        disclosure_class="explicit_provider",
        admitted_provider="openrouter",
        retention="session",
    )
    reference = record.reference()
    user_content = UserContent(parts=[ImagePart(artifact=reference)])
    message = Message(role="user", content=list(user_content.parts))
    serialized = message.model_dump_json()

    assert store.root.is_dir()
    assert "synthetic_fixture" not in serialized
    assert "openrouter" not in serialized
    assert "relative_path" not in serialized
    assert "retention" not in serialized
    assert "base64" not in serialized
    assert str(settings.user_data_dir) not in serialized

    reopened = SessionMediaStore.create(settings, session.id)
    resolved = await reopened.resolver(
        session,
        provider="openrouter",
        profile_scope=scope,
    ).resolve(reference)
    assert resolved.content == content
    assert resolved.sha256 == reference.sha256
    assert (resolved.width, resolved.height) == (3, 2)
    assert not (tmp_path / "project-data").exists()

    await reopened.remove_all(session)
    assert session.media == []
    assert not reopened.root.exists()


async def test_synthetic_non_browser_image_projects_and_encodes_through_shared_core(
    tmp_path,
) -> None:
    settings = _settings(tmp_path)
    scope = settings.resolve_profile_scope()
    session = AgentSession.create(settings, profile_scope=scope, provider="openrouter")
    store = SessionMediaStore.create(settings, session.id)
    content = _png()
    record = await store.admit_png(
        session,
        content=content,
        source_label=ProfileLabel.owned_by(scope.primary),
        source_owner=scope.primary,
        provenance="synthetic_fixture",
        disclosure_class="explicit_provider",
        admitted_provider="openrouter",
        retention="session",
    )
    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn_synthetic_media",
        iteration=1,
        user_input=UserContent(parts=[ImagePart(artifact=record.reference())]),
    )

    payload = await to_openrouter_request_with_media(
        assembly.request,
        store.resolver(session, provider="openrouter", profile_scope=scope),
    )

    assert assembly.report.projected_image_count == 1
    assert assembly.report.projected_image_bytes == len(content)
    image_payload = payload["messages"][-1]["content"][0]
    assert image_payload["type"] == "image_url"
    assert image_payload["image_url"]["url"].startswith("data:image/png;base64,")
    assert str(store.root) not in str(payload)


async def test_media_resolver_rejects_wrong_authority_policy_and_tampering(tmp_path) -> None:
    settings = _settings(
        tmp_path,
        profile_configs={"personal": {"browser": {"screenshot_allowed_providers": ["openrouter"]}}},
    )
    scope = settings.resolve_profile_scope()
    session = AgentSession.create(settings, profile_scope=scope, provider="openrouter")
    store = SessionMediaStore.create(settings, session.id)
    record = await store.admit_png(
        session,
        content=_png(),
        source_label=ProfileLabel.owned_by("personal"),
        source_owner="personal",
        provenance="browser_screenshot",
        disclosure_class="browser_screenshot",
        admitted_provider="openrouter",
    )

    with pytest.raises(SessionMediaError, match="wrong session authority|does not match"):
        await store.materialize(
            session,
            record.reference(),
            provider="anthropic",
            profile_scope=scope,
        )
    with pytest.raises(SessionMediaError, match="wrong session authority|does not match"):
        store.resolver(session, provider="anthropic", profile_scope=scope)
    with pytest.raises(SessionMediaError, match="does not match the session"):
        store.resolver(
            session,
            provider="openrouter",
            profile_scope=ProfileScope.create("shared"),
        )

    configured = settings.profile_configs["personal"].browser
    assert configured is not None
    configured.screenshot_allowed_providers.clear()
    with pytest.raises(SessionMediaError, match="policy denies"):
        await store.materialize(
            session,
            record.reference(),
            provider="openrouter",
            profile_scope=scope,
        )

    configured.screenshot_allowed_providers.append("openrouter")
    path = store.root / record.relative_path
    tampered = bytearray(path.read_bytes())
    tampered[-1] ^= 1
    path.write_bytes(tampered)
    with pytest.raises(SessionMediaError, match="digest mismatch"):
        await store.materialize(
            session,
            record.reference(),
            provider="openrouter",
            profile_scope=scope,
        )


async def test_media_limits_profile_scope_and_runtime_cleanup(tmp_path) -> None:
    content = _png()
    settings = _settings(
        tmp_path,
        context={"media": {"session_byte_limit": len(content) - 1}},
    )
    scope = settings.resolve_profile_scope()
    session = AgentSession.create(settings, profile_scope=scope, provider="openrouter")
    store = SessionMediaStore.create(settings, session.id)

    with pytest.raises(SessionMediaLimitError, match="session byte limit"):
        await store.admit_png(
            session,
            content=content,
            source_label=ProfileLabel.owned_by(scope.primary),
            source_owner=scope.primary,
            provenance="synthetic_fixture",
            disclosure_class="explicit_provider",
            admitted_provider="openrouter",
        )
    assert session.media == []
    assert not store.root.exists()

    other = AgentSession.create(
        settings,
        profile_scope=scope,
        provider="openrouter",
    )
    with pytest.raises(SessionMediaError, match="does not belong"):
        await store.admit_png(
            other,
            content=content,
            source_label=ProfileLabel.owned_by(scope.primary),
            source_owner=scope.primary,
            provenance="synthetic_fixture",
            disclosure_class="explicit_provider",
            admitted_provider="openrouter",
        )


async def test_runtime_cleanup_removes_only_runtime_media(tmp_path) -> None:
    settings = _settings(tmp_path)
    scope = settings.resolve_profile_scope()
    session = AgentSession.create(settings, profile_scope=scope, provider="openrouter")
    store = SessionMediaStore.create(settings, session.id)
    common = {
        "source_label": ProfileLabel.owned_by(scope.primary),
        "source_owner": scope.primary,
        "provenance": "synthetic_fixture",
        "disclosure_class": "explicit_provider",
        "admitted_provider": "openrouter",
    }
    runtime = await store.admit_png(
        session,
        content=_png(color=(1, 2, 3)),
        retention="runtime",
        **common,
    )
    retained = await store.admit_png(
        session,
        content=_png(color=(4, 5, 6)),
        retention="session",
        **common,
    )

    await store.remove_retention(session, "runtime")

    assert session.media == [retained]
    assert not (store.root / runtime.relative_path).exists()
    assert (store.root / retained.relative_path).is_file()
    resolver = store.resolver(session, provider="openrouter", profile_scope=scope)
    with pytest.raises(SessionMediaError, match="unknown media artifact"):
        await resolver.resolve(runtime.reference())
    assert (await resolver.resolve(retained.reference())).sha256 == retained.sha256


async def test_media_admission_cancellation_removes_completed_atomic_write(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    scope = settings.resolve_profile_scope()
    session = AgentSession.create(settings, profile_scope=scope, provider="openrouter")
    store = SessionMediaStore.create(settings, session.id)
    started = asyncio.Event()
    release = asyncio.Event()
    original = store._write_atomic

    def delayed_write(relative_path: str, content: bytes) -> None:
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
        original(relative_path, content)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(store, "_write_atomic", delayed_write)
    task = asyncio.create_task(
        store.admit_png(
            session,
            content=_png(),
            source_label=ProfileLabel.owned_by(scope.primary),
            source_owner=scope.primary,
            provenance="synthetic_fixture",
            disclosure_class="explicit_provider",
            admitted_provider="openrouter",
        )
    )
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.media == []
    assert not store.root.exists() or not any(store.root.iterdir())


async def test_media_reset_joins_namespace_deletion_before_rebinding_on_cancellation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    scope = settings.resolve_profile_scope()
    session = AgentSession.create(settings, profile_scope=scope, provider="openrouter")
    fresh = AgentSession.create(settings, profile_scope=scope, provider="openrouter")
    store = SessionMediaStore.create(settings, session.id)
    await store.admit_png(
        session,
        content=_png(),
        source_label=ProfileLabel.owned_by(scope.primary),
        source_owner=scope.primary,
        provenance="synthetic_fixture",
        disclosure_class="explicit_provider",
        admitted_provider="openrouter",
    )
    old_root = store.root
    started = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = store._remove_all_sync

    def delayed_deletion() -> None:
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
        original()

    monkeypatch.setattr(store, "_remove_all_sync", delayed_deletion)
    task = asyncio.create_task(store.reset_for_session(session, fresh.id))
    await started.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert session.media
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.media == []
    assert not old_root.exists()
    assert store.root.parent.name == fresh.id
