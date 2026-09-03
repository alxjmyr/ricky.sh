"""Durable gateway conversation and inbound-result store tests."""

from pathlib import Path

import pytest

import ricky.gateway.upgrade as gateway_upgrade
from ricky.config import GatewaySettings, RickySettings
from ricky.gateway.store import (
    ConversationConflictError,
    GatewayResultConflictError,
    GatewayStore,
)
from ricky.gateway.types import ConversationKey
from ricky.profiles import ProfileLabel, ProfileScope

_SCOPE = ProfileScope.create("personal")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        gateway=GatewaySettings(),
    )


async def test_same_key_resumes_after_store_restart_and_resolves_trusted_route(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = GatewayStore(settings)
    await first.initialize()
    key = ConversationKey(
        transport="telegram",
        account="personal",
        destination_id="200",
        thread_id="topic",
    )
    created = await first.create(
        key=key,
        session_id="session_" + "a" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=_SCOPE,
        project_root=None,
    )

    restarted = GatewayStore(settings)
    await restarted.initialize()
    assert await restarted.get_active(key, scope=_SCOPE) == created
    resolved = await restarted.resolve_conversation_route(created.id, _SCOPE.label())
    assert resolved.route == f"conversation:{created.id}"
    assert resolved.transport == "telegram"
    assert resolved.account == "personal"
    assert resolved.destination_ref == "200"


async def test_one_active_mapping_and_inbound_result_are_revision_fenced(
    tmp_path: Path,
) -> None:
    store = GatewayStore(_settings(tmp_path))
    await store.initialize()
    key = ConversationKey(
        transport="telegram",
        account="personal",
        destination_id="200",
    )
    conversation = await store.create(
        key=key,
        session_id="session_" + "a" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=_SCOPE,
        project_root=None,
    )
    with pytest.raises(ConversationConflictError):
        await store.create(
            key=key,
            session_id="session_" + "b" * 32,
            route_name="owner",
            provider="openrouter",
            model="model",
            profile_scope=_SCOPE,
            project_root=None,
        )

    message_id = "inbound_" + "c" * 32
    await store.begin_result(
        message_id=message_id,
        conversation_id=conversation.id,
        session_id=conversation.session_id,
        scope=_SCOPE,
    )
    with pytest.raises(GatewayResultConflictError):
        await store.begin_result(
            message_id=message_id,
            conversation_id=conversation.id,
            session_id=conversation.session_id,
            scope=_SCOPE,
        )
    changed, result = await store.finish_result(
        message_id=message_id,
        conversation_id=conversation.id,
        expected_conversation_revision=0,
        status="committed",
        session_revision=1,
        response_outbox_id="outbox_" + "d" * 32,
        error=None,
        scope=_SCOPE,
    )
    assert changed.revision == 1
    assert changed.last_processed_inbound_message_id == message_id
    assert result.status == "committed"
    with pytest.raises(GatewayResultConflictError):
        await store.finish_result(
            message_id=message_id,
            conversation_id=conversation.id,
            expected_conversation_revision=0,
            status="committed",
            session_revision=1,
            response_outbox_id="outbox_" + "d" * 32,
            error=None,
            scope=_SCOPE,
        )


async def test_archive_allows_a_new_active_mapping_without_deleting_audit(
    tmp_path: Path,
) -> None:
    store = GatewayStore(_settings(tmp_path))
    await store.initialize()
    key = ConversationKey(
        transport="telegram",
        account="personal",
        destination_id="200",
    )
    old = await store.create(
        key=key,
        session_id="session_" + "a" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=_SCOPE,
        project_root=None,
    )
    archived = await store.archive(old.id, scope=_SCOPE, expected_revision=0)
    new = await store.create(
        key=key,
        session_id="session_" + "b" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=_SCOPE,
        project_root=None,
    )

    assert archived.status == "archived"
    assert (await store.get(old.id, scope=_SCOPE)).status == "archived"
    assert (await store.get_active(key, scope=_SCOPE)).id == new.id  # type: ignore[union-attr]


async def test_store_filters_and_rejects_inaccessible_profile_records(tmp_path: Path) -> None:
    store = GatewayStore(_settings(tmp_path))
    await store.initialize()
    work_scope = ProfileScope.create("work")
    conversation = await store.create(
        key=ConversationKey(
            transport="telegram",
            account="work",
            destination_id="201",
        ),
        session_id="session_" + "e" * 32,
        route_name="work",
        provider="openrouter",
        model="model",
        profile_scope=work_scope,
        project_root=None,
    )

    assert await store.list(scope=_SCOPE) == []
    with pytest.raises(Exception, match="not found"):
        await store.get(conversation.id, scope=_SCOPE)
    with pytest.raises(Exception, match="not found"):
        await store.resolve_conversation_route(
            conversation.id,
            ProfileLabel(required_profiles=("shared", "personal")),
        )


async def test_initialize_creates_the_schema_when_inspection_reports_it_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that disappears under the exists() test must still be created.

    ``inspect_gateway_store`` reports ``exists=False`` for a missing database
    rather than raising, so discarding its result would let ``_initialize``
    return with no schema at all.
    """

    settings = _settings(tmp_path)
    await GatewayStore(settings).initialize()

    created: list[Path] = []

    def report_absent(path: Path) -> gateway_upgrade.GatewayStoreInspection:
        return gateway_upgrade.GatewayStoreInspection(path=str(path), exists=False, size=0)

    def record_create(path: Path) -> None:
        created.append(path)

    monkeypatch.setattr(gateway_upgrade, "inspect_gateway_store", report_absent)
    monkeypatch.setattr(gateway_upgrade, "create_current_gateway_store", record_create)

    await GatewayStore(settings).initialize()

    assert created, "an absent inspection must lead to a create, not a silent return"
