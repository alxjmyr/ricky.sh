"""Trusted notification reply correlation and bounded context tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ricky.capabilities import GuardrailIntakeField, GuardrailIntakeSpec
from ricky.config import GatewaySettings, MessagingSettings, RickySettings
from ricky.durable_tasks.store import DurableTaskStore
from ricky.executions.store import ExecutionStore
from ricky.executions.types import ExecutionRequest
from ricky.gateway.context import (
    GatewayContextError,
    GatewayContextLoader,
    gateway_instructions,
    render_gateway_activity,
)
from ricky.gateway.store import GatewayStore
from ricky.gateway.types import (
    Conversation,
    ConversationKey,
    GatewayCapabilityCatalog,
    GatewayCapabilityItem,
)
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import DeliveryReceipt, InboundMessage, TransportMessage
from ricky.notifications.store import NotificationStore
from ricky.notifications.types import CorrelationRef, NotificationRequest
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.workflows.run import StepRecord, WorkflowRun, WorkflowSourceIdentity
from ricky.workflows.run_store import WorkflowRunStore

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
SCOPE = ProfileScope.create("personal")


def test_gateway_instructions_expose_exact_guardrail_intake_before_delegation() -> None:
    conversation = Conversation(
        id="conversation_" + "a" * 32,
        key=ConversationKey(
            transport="telegram",
            account="personal",
            destination_id="200",
        ),
        session_id="session_" + "b" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        project_root=None,
        status="active",
        revision=0,
        created_at=NOW,
        updated_at=NOW,
    )
    inbound = InboundMessage(
        id="inbound_" + "c" * 32,
        transport="telegram",
        account="personal",
        update_id="1",
        destination_id="200",
        sender_id="100",
        platform_message_id="300",
        text="Book dinner between 18:00 and 20:00.",
        received_at=NOW,
        status="pending",
    )
    catalog = GatewayCapabilityCatalog(
        ad_hoc_capabilities=[
            GatewayCapabilityItem(
                name="builtin.sandbox.reservation",
                description="Create a sandbox reservation.",
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
            )
        ]
    )

    instructions = gateway_instructions(
        conversation,
        inbound,
        catalog=catalog,
        capabilities=["builtin.sandbox.reservation"],
    )

    assert '"window_start"' in instructions
    assert '"format": "HH:MM"' in instructions
    assert "action=supply_guardrails" in instructions
    assert "source_quote is audit context only" in instructions
    assert "translating natural-language" in instructions
    assert "prior fields remain durable" in instructions
    assert "mobile-first portable Markdown" in instructions
    assert "tables to at most three short columns" in instructions
    assert "Do not emit raw HTML" in instructions


def test_gateway_instructions_require_complete_background_capability_set() -> None:
    conversation = Conversation(
        id="conversation_" + "a" * 32,
        key=ConversationKey(
            transport="telegram",
            account="personal",
            destination_id="200",
        ),
        session_id="session_" + "b" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        project_root=None,
        status="active",
        revision=0,
        created_at=NOW,
        updated_at=NOW,
    )
    inbound = InboundMessage(
        id="inbound_" + "c" * 32,
        transport="telegram",
        account="personal",
        update_id="1",
        destination_id="200",
        sender_id="100",
        platform_message_id="300",
        text="Decline my conflicting calendar events in the background.",
        received_at=NOW,
        status="pending",
    )
    catalog = GatewayCapabilityCatalog(
        ad_hoc_capabilities=[
            GatewayCapabilityItem(
                name="builtin.calendar.read",
                description="Read calendar events.",
            ),
            GatewayCapabilityItem(
                name="builtin.calendar.mutate",
                description="Respond to calendar invitations.",
            ),
        ]
    )

    instructions = gateway_instructions(
        conversation,
        inbound,
        catalog=catalog,
        capabilities=["builtin.automation.mutate"],
    )

    assert "complete the task end-to-end from persisted task context" in instructions
    assert "Include prerequisite read or discovery capabilities" in instructions
    assert "never assume mutation implies read access" in instructions


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings(body_char_limit=20_000),
        gateway=GatewaySettings(recent_activity_limit=5, activity_char_limit=4_000),
    )


async def _delivered_notification(
    settings: RickySettings,
    conversation_id: str,
    task_id: str,
    task_revision: int,
) -> tuple[str, str]:
    notifications = NotificationStore(settings)
    messaging = MessagingStore(settings)
    await notifications.initialize()
    await messaging.initialize()
    record = await notifications.enqueue(
        NotificationRequest(
            id="notification_" + "a" * 32,
            route=f"conversation:{conversation_id}",
            body="Background result",
            source_kind="execution_result",
            profile_label=SCOPE.label(),
            source_id="execution_" + "b" * 32,
            dedupe_key="result",
            correlations=[
                CorrelationRef(
                    kind="task",
                    id=task_id,
                    revision=task_revision,
                    profile_label=SCOPE.label(),
                ),
                CorrelationRef(
                    kind="conversation",
                    id=conversation_id,
                    revision=0,
                    profile_label=SCOPE.label(),
                ),
            ],
            created_at=NOW,
        ),
        scope=SCOPE,
    )
    claimed = await notifications.claim(
        record.outbox.id,
        worker="test",
        transport="telegram",
        destination_ref="200",
        scope=SCOPE,
    )
    outbound = TransportMessage(
        id="transport_message_" + "c" * 32,
        transport="telegram",
        account="personal",
        destination_id="200",
        text="Background result",
        outbox_id=claimed.id,
        part_number=1,
        part_count=1,
    )
    await messaging.prepare_parts(claimed, [outbound])
    receipt = DeliveryReceipt(
        transport="telegram",
        account="personal",
        transport_message_id=outbound.id,
        platform_message_id="900",
        destination_id="200",
        delivered_at=NOW,
    )
    await messaging.record_receipt(claimed, receipt)
    await notifications.mark_delivered(claimed, scope=SCOPE, platform_message_id="900")
    return record.request.id, record.outbox.id


async def test_reply_loads_exact_linked_current_records_without_background_transcript(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    gateway = GatewayStore(settings)
    await gateway.initialize()
    conversation = await gateway.create(
        key=ConversationKey(
            transport="telegram",
            account="personal",
            destination_id="200",
        ),
        session_id="session_" + "d" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        project_root=None,
    )
    tasks = await DurableTaskStore.create(settings, profile="personal")
    task = await tasks.create_task(
        title="Research",
        objective="Find the answer",
        closure_criteria="Cited result",
        execution_mode="joint",
        authority="direct_user_instruction",
        executor_id="test",
    )
    notification_id, _ = await _delivered_notification(
        settings, conversation.id, task.id, task.revision
    )
    inbound = InboundMessage(
        id="inbound_" + "e" * 32,
        transport="telegram",
        account="personal",
        update_id="10",
        destination_id="200",
        sender_id="100",
        platform_message_id="901",
        reply_to_platform_message_id="900",
        text="Use that result",
        received_at=NOW,
        status="pending",
    )
    loader = GatewayContextLoader(settings, gateway=gateway)
    activity = await loader.load(inbound, conversation)

    assert activity.replied_notification_id == notification_id
    linked_task = next(item for item in activity.records if item.kind == "task")
    assert linked_task.id == task.id
    assert linked_task.revision == task.revision
    rendered = render_gateway_activity(activity, char_limit=4_000)
    assert "<gateway_activity>" in rendered
    assert "transcript" not in rendered.lower()
    assert "Background result" in rendered


async def test_unknown_platform_reply_is_not_treated_as_correlation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    gateway = GatewayStore(settings)
    await gateway.initialize()
    conversation = await gateway.create(
        key=ConversationKey(
            transport="telegram",
            account="personal",
            destination_id="200",
        ),
        session_id="session_" + "d" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        project_root=None,
    )
    inbound = InboundMessage(
        id="inbound_" + "e" * 32,
        transport="telegram",
        account="personal",
        update_id="10",
        destination_id="200",
        sender_id="100",
        platform_message_id="901",
        reply_to_platform_message_id="not-ricky",
        text="Use that",
        received_at=NOW,
        status="pending",
    )
    with pytest.raises(GatewayContextError, match="trusted Ricky delivery"):
        await GatewayContextLoader(settings, gateway=gateway).load(inbound, conversation)


async def test_workflow_correlation_loads_with_conversation_profile_scope(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    conversation = Conversation(
        id="conversation_" + "a" * 32,
        key=ConversationKey(
            transport="telegram",
            account="personal",
            destination_id="200",
        ),
        session_id="session_" + "b" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        project_root=None,
        status="active",
        revision=0,
        created_at=NOW,
        updated_at=NOW,
    )
    run = WorkflowRun(
        id="workflow_profilescope",
        workflow_name="triage",
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="triage"),
            path="/synthetic/workflow.toml",
            scope="fixture",
            content_digest="digest",
        ),
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        storage_scope="user",
        graph_fingerprint="fingerprint",
        steps={
            "done": StepRecord(
                step_id="done",
                execution_address="done",
                kind="message",
            )
        },
    )
    store = WorkflowRunStore(settings, project_root=tmp_path)
    await store.save(run)
    loader = GatewayContextLoader(settings, project_root=tmp_path)

    correlated = await loader._load_ref(
        CorrelationRef(
            kind="workflow_run",
            id=run.id,
            profile_label=SCOPE.label(),
        ),
        conversation,
    )

    assert correlated.kind == "workflow_run"
    assert correlated.id == run.id


async def test_job_and_execution_correlations_load_labeled_durable_records(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    conversation = Conversation(
        id="conversation_" + "a" * 32,
        key=ConversationKey(
            transport="telegram",
            account="personal",
            destination_id="200",
        ),
        session_id="session_" + "b" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        project_root=None,
        status="active",
        revision=0,
        created_at=NOW,
        updated_at=NOW,
    )
    job = JobRun(
        id="jobrun_context",
        job_name="personal/context",
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        session_id="session_job_context",
        outcome="succeeded",
        started_at=NOW,
        finished_at=NOW,
        final_message="done",
    )
    jobs = JobRunStore(settings)
    await jobs.initialize()
    await jobs.insert(job, scope=SCOPE)
    request = ExecutionRequest(
        id="execution_" + "c" * 32,
        kind="named_job",
        status="queued",
        named_job="personal/context",
        job_digest="d" * 64,
        profile_scope=SCOPE,
        notification_route="owner",
        request_key="context",
        created_at=NOW,
    )
    executions = ExecutionStore(settings)
    await executions.initialize()
    await executions.submit(request, scope=SCOPE)
    loader = GatewayContextLoader(settings)

    records = [
        await loader._load_ref(
            CorrelationRef(
                kind="job_run",
                id=job.id,
                profile_label=SCOPE.label(),
            ),
            conversation,
        ),
        await loader._load_ref(
            CorrelationRef(
                kind="execution_request",
                id=request.id,
                profile_label=SCOPE.label(),
            ),
            conversation,
        ),
    ]

    assert [(record.kind, record.id) for record in records] == [
        ("job_run", job.id),
        ("execution_request", request.id),
    ]


async def test_workflow_correlation_hides_out_of_scope_run_as_not_found(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    conversation = Conversation(
        id="conversation_" + "a" * 32,
        key=ConversationKey(
            transport="telegram",
            account="personal",
            destination_id="200",
        ),
        session_id="session_" + "b" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        project_root=None,
        status="active",
        revision=0,
        created_at=NOW,
        updated_at=NOW,
    )
    work_scope = ProfileScope.create("work")
    run = WorkflowRun(
        id="workflow_outofscope",
        workflow_name="triage",
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="work", name="triage"),
            path="/synthetic/workflow.toml",
            scope="fixture",
            content_digest="digest",
        ),
        provider="claude_code",
        model="sonnet",
        profile_scope=work_scope,
        storage_scope="user",
        graph_fingerprint="fingerprint",
        steps={},
    )
    await WorkflowRunStore(settings, project_root=tmp_path).save(run)

    with pytest.raises(GatewayContextError, match="workflow run not found"):
        await GatewayContextLoader(settings, project_root=tmp_path)._load_ref(
            CorrelationRef(
                kind="workflow_run",
                id=run.id,
                profile_label=SCOPE.label(),
            ),
            conversation,
        )
