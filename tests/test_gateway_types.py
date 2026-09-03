"""Gateway boundary model tests."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ricky.capabilities import GuardrailIntakeField, GuardrailIntakeSpec
from ricky.gateway.types import (
    Conversation,
    ConversationKey,
    CorrelatedRecord,
    GatewayActivity,
    GatewayCapabilityCatalog,
    GatewayCapabilityItem,
    GatewayInboundResult,
)
from ricky.profiles import ProfileLabel, ProfileScope


def test_gateway_models_survive_strict_json_round_trip() -> None:
    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    key = ConversationKey(
        transport="telegram",
        account="personal",
        destination_id="200",
        thread_id="topic",
    )
    conversation = Conversation(
        id="conversation_" + "a" * 32,
        key=key,
        session_id="session_" + "b" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=ProfileScope.create("personal"),
        project_root=None,
        status="active",
        revision=2,
        created_at=now,
        updated_at=now,
        last_processed_inbound_message_id="inbound_" + "c" * 32,
    )
    assert Conversation.model_validate_json(conversation.model_dump_json()) == conversation
    assert ConversationKey.model_validate_json(key.model_dump_json()).digest() == key.digest()

    result = GatewayInboundResult(
        message_id="inbound_" + "c" * 32,
        conversation_id=conversation.id,
        session_id=conversation.session_id,
        profile_label=conversation.profile_scope.label(),
        status="committed",
        session_revision=3,
        response_outbox_id="outbox_" + "d" * 32,
        started_at=now,
        finished_at=now,
    )
    assert GatewayInboundResult.model_validate_json(result.model_dump_json()) == result

    activity = GatewayActivity(
        profile_label=conversation.profile_scope.label(),
        records=[
            CorrelatedRecord(
                kind="task",
                id="task_" + "e" * 32,
                revision=4,
                status="waiting",
                summary="Waiting for the user",
                profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            )
        ],
    )
    assert GatewayActivity.model_validate_json(activity.model_dump_json()) == activity


def test_conversation_identity_includes_thread_and_models_reject_extra_fields() -> None:
    root = ConversationKey(
        transport="telegram",
        account="personal",
        destination_id="200",
    )
    thread = root.model_copy(update={"thread_id": "99"})
    assert root.digest() != thread.digest()

    with pytest.raises(ValidationError, match="Extra inputs"):
        ConversationKey.model_validate(
            {
                "transport": "telegram",
                "account": "personal",
                "destination_id": "200",
                "sender_id": "not-part-of-identity",
            }
        )


def test_gateway_capability_catalog_survives_strict_json_round_trip() -> None:
    catalog = GatewayCapabilityCatalog(
        named_jobs=[GatewayCapabilityItem(name="project-brief", description="Summarize project")],
        ad_hoc_capabilities=[
            GatewayCapabilityItem(name="builtin.project.read", description="Read project files"),
            GatewayCapabilityItem(
                name="builtin.sandbox.reservation",
                description="Create one sandbox reservation",
                confirmation_required=True,
                guardrail_required=True,
                guardrail_intake=GuardrailIntakeSpec(
                    schema_id="sandbox.reservation",
                    schema_version=1,
                    fields=(
                        GuardrailIntakeField(
                            name="window_start",
                            value_type="time",
                            description="Earliest acceptable arrival time.",
                            question="What is the earliest acceptable arrival time?",
                            format="HH:MM",
                        ),
                    ),
                ),
            ),
        ],
    )

    assert GatewayCapabilityCatalog.model_validate_json(catalog.model_dump_json()) == catalog

    with pytest.raises(ValidationError, match="intake specification"):
        GatewayCapabilityItem(
            name="builtin.missing",
            description="Missing evaluator metadata",
            guardrail_required=True,
        )
