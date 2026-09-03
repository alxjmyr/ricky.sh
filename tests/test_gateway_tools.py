"""Foreground capability ceiling and stale revision tests."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from ricky.agent.session import AgentSession
from ricky.config import (
    GatewayRouteSettings,
    GatewaySettings,
    MessagingRouteSettings,
    MessagingSettings,
    MessagingTransportSettings,
    RickySettings,
    TelegramAccountSettings,
)
from ricky.durable_tasks.store import DurableTaskStore
from ricky.executions.drafts import AdHocExecutionProposal
from ricky.executions.tools import StartNamedJobTool as NeutralStartNamedJobTool
from ricky.gateway.conversations import build_gateway_runtime
from ricky.gateway.tools import (
    DelegateTaskParams,
    gateway_capability_inventory_tools,
    gateway_control_descriptors,
)
from ricky.gateway.types import (
    Conversation,
    ConversationKey,
    CorrelatedRecord,
    GatewayActivity,
)
from ricky.llm import CompletionRequest, StreamEvent
from ricky.messaging.types import InboundMessage
from ricky.profiles import ProfileScope
from ricky.tools import Tool, ToolContext

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
SCOPE = ProfileScope.create("personal")


class NoCallProvider:
    name = "fake"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []
        self.closed = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        if False:
            yield cast(Any, None)
        raise AssertionError("provider should not be called")

    async def aclose(self) -> None:
        self.closed = True


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings(
            telegram_accounts={
                "personal/bot": TelegramAccountSettings(
                    bot_token=SecretStr("token"),
                    allowed_sender_ids=["100"],
                    allowed_destination_ids=["200"],
                )
            },
            transports={
                "owner-telegram": MessagingTransportSettings(
                    type="telegram", account="personal/bot"
                )
            },
            routes={
                "owner": MessagingRouteSettings(
                    transport="owner-telegram",
                    destination="200",
                    owner_profile="personal",
                    accepted_profiles=["shared", "personal"],
                )
            },
        ),
        gateway=GatewaySettings(
            enabled=True,
            routes={
                "owner": GatewayRouteSettings(
                    provider="openrouter",
                    model="model",
                    primary_profile="personal",
                    project_root=str(tmp_path),
                )
            },
        ),
    )


def _conversation(session: AgentSession) -> Conversation:
    return Conversation(
        id="conversation_" + "a" * 32,
        key=ConversationKey(
            transport="telegram",
            account="personal/bot",
            destination_id="200",
        ),
        session_id=session.id,
        route_name="owner",
        provider=session.provider,
        model=session.model,
        profile_scope=session.profile_scope,
        project_root=None,
        status="active",
        revision=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _inbound() -> InboundMessage:
    return InboundMessage(
        id="inbound_" + "b" * 32,
        transport="telegram",
        account="personal/bot",
        update_id="1",
        destination_id="200",
        sender_id="100",
        platform_message_id="300",
        text="research this",
        received_at=NOW,
        status="pending",
    )


def test_delegate_task_schema_exposes_one_top_level_discriminated_command() -> None:
    raw_schema = DelegateTaskParams.model_json_schema()
    schema = json.dumps(raw_schema)
    assert raw_schema["discriminator"]["propertyName"] == "action"
    assert "proposal" not in schema
    assert all(action in schema for action in ("start", "supply_guardrails", "confirm", "cancel"))
    for hidden in (
        "route",
        "destination",
        "principal_id",
        "source_message_id",
        "request_key",
    ):
        assert hidden not in schema

    started = DelegateTaskParams.model_validate(
        {
            "action": "start",
            "task_id": "task_" + "c" * 32,
            "expected_task_revision": 1,
            "goal": "Use one guarded capability.",
            "requested_capabilities": ["builtin.sandbox.reservation"],
            "guardrails": [
                {
                    "capability_id": "builtin.sandbox.reservation",
                    "fields": [{"field": "party_size", "value": 2}],
                }
            ],
        },
        strict=True,
    )
    assert isinstance(started.root, AdHocExecutionProposal)
    assert started.root.requested_capabilities == ["builtin.sandbox.reservation"]
    assert started.root.guardrails[0].fields[0].value == 2
    assert DelegateTaskParams.model_validate_json(started.model_dump_json(), strict=True) == started

    confirmed = DelegateTaskParams.model_validate(
        {
            "action": "confirm",
            "draft_id": "draft_" + "a" * 32,
            "expected_draft_revision": 3,
        }
    )
    assert confirmed.root.action == "confirm"
    with pytest.raises(ValueError):
        DelegateTaskParams.model_validate(
            {
                "action": "confirm",
                "draft_id": "draft_" + "a" * 32,
                "expected_draft_revision": 3,
                "goal": "silently changed",
            }
        )

    cancelled = DelegateTaskParams.model_validate(
        {
            "action": "cancel",
            "draft_id": "draft_" + "b" * 32,
            "expected_draft_revision": 2,
        }
    )
    assert cancelled.root.action == "cancel"

    # Live acceptance exposed Gemini stringifying the former nested object.
    # The obsolete envelope is deliberately unsupported rather than migrated.
    with pytest.raises(ValueError):
        DelegateTaskParams.model_validate(
            {
                "proposal": (
                    '{"action":"confirm","draft_id":"draft_'
                    + "c" * 32
                    + '","expected_draft_revision":1}'
                )
            }
        )


def test_gateway_capability_inventory_does_not_replace_neutral_tool_schema() -> None:
    neutral = cast(Tool, NeutralStartNamedJobTool.__new__(NeutralStartNamedJobTool))

    merged = gateway_capability_inventory_tools(
        (neutral,),
        gateway_control_descriptors(),
    )
    by_name = {tool.name: tool for tool in merged}

    assert by_name["start_named_job"] is neutral
    assert "prepare_capability_use" in by_name


async def test_gateway_runtime_excludes_external_mutation_shell_and_raw_send(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(
        settings, profile_scope=SCOPE, provider="openrouter", model="model"
    )
    provider = NoCallProvider()
    async with build_gateway_runtime(
        settings,
        session=session,
        conversation=_conversation(session),
        inbound=_inbound(),
        activity=GatewayActivity(profile_label=SCOPE.label()),
        provider=provider,
    ) as runtime:
        names = {tool.name for tool in runtime.registry.tools()}
        assert "run_shell" not in names
        assert "notify_user" not in names
        assert "write_file" not in names
        assert "edit_file" not in names
        assert "start_workflow" not in names
        assert "create_durable_task" in names
        assert "delegate_task" in names
        assert "create_execution_request" not in names
        assert "start_named_job" in names
        assert all(
            tool.risk == "read_only"
            or tool.name
            in {
                "create_durable_task",
                "park_for_review",
                "claim_durable_task",
                "renew_durable_task_lease",
                "update_durable_task_progress",
                "update_durable_task_tags",
                "wait_durable_task",
                "block_durable_task",
                "complete_durable_task",
                "cancel_durable_task",
                "reopen_durable_task",
                "release_durable_task",
                "write_task_artifact",
                "edit_task_artifact",
                "start_named_job",
                "delegate_task",
                "prepare_capability_use",
                "cancel_execution_request",
            }
            for tool in runtime.registry.tools()
        )
    assert provider.closed


async def test_stale_correlated_task_revision_fails_before_state_change(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(
        settings, profile_scope=SCOPE, provider="openrouter", model="model"
    )
    tasks = await DurableTaskStore.create(settings, profile="personal")
    task = await tasks.create_task(
        title="Decision",
        objective="Apply answer",
        closure_criteria="Answer applied",
        execution_mode="joint",
        authority="direct_user_instruction",
        executor_id="setup",
    )
    activity = GatewayActivity(
        profile_label=SCOPE.label(),
        records=[
            CorrelatedRecord(
                kind="task",
                id=task.id,
                revision=task.revision,
                status=task.status,
                summary="revision one",
                profile_label=SCOPE.label(),
            )
        ],
    )
    provider = NoCallProvider()
    async with build_gateway_runtime(
        settings,
        session=session,
        conversation=_conversation(session),
        inbound=_inbound(),
        activity=activity,
        provider=provider,
    ) as runtime:
        changed = await tasks.claim(
            task.id,
            holder_session_id="session_" + "f" * 32,
            authority="joint_work",
            executor_id="other",
            expected_revision=task.revision,
        )
        result = await runtime.registry.dispatch(
            "claim_durable_task",
            {"task_id": task.id, "expected_revision": task.revision},
            ToolContext(cwd=tmp_path, settings=settings, session=session),
        )
        assert result.is_error
        assert "stale task revision" in result.content
        assert (await tasks.get_task(task.id)).revision == changed.revision
