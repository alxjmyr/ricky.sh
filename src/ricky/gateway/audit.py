"""Read-only audit projection across subsystem-owned records.

Each subsystem keeps ownership of its own storage. This module only follows
canonical ids and reports what it finds. It copies no transcript body, resolves
nothing by similarity, and renders an absent link as ``missing`` rather than
guessing a plausible one.

The chain combines gateway operations with execution-contract evidence:

    inbound message -> conversation and foreground turn -> durable task
    -> execution draft -> guardrail/confirmation -> execution contract
    -> delegation grant -> execution request -> job run and transcript
    -> external action receipt -> notification and delivery receipt
    -> correlated user reply
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ricky.authority.store import AuthorityStore
from ricky.config import RickySettings
from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.executions.drafts import ExecutionDraft
from ricky.executions.store import ExecutionStore
from ricky.gateway.store import GatewayStore
from ricky.jobs.store import JobRunStore
from ricky.messaging.store import MessagingStore
from ricky.notifications.store import NotificationStore
from ricky.profiles import ProfileScope

AuditLinkKind = Literal[
    "inbound_message",
    "conversation",
    "foreground_turn",
    "durable_task",
    "execution_draft",
    "guardrail",
    "confirmation",
    "execution_contract",
    "delegation_grant",
    "execution_request",
    "job_run",
    "external_action",
    "notification",
    "delivery_receipt",
    "user_reply",
]
LinkState = Literal["present", "missing", "unreadable"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditLink(_FrozenModel):
    """One node in the audit chain, present or honestly absent."""

    kind: AuditLinkKind
    state: LinkState
    id: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, max_length=100)
    at: datetime | None = None
    detail: str = Field(min_length=1, max_length=2_000)
    """Bounded canonical facts only. Never a transcript or model output body."""


class AuditChain(_FrozenModel):
    """The complete projection for one correlation id."""

    generated_at: datetime
    query: str = Field(min_length=1, max_length=500)
    resolved_kind: AuditLinkKind | None = None
    links: tuple[AuditLink, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("generated_at must be timezone-aware UTC")
        return value

    @property
    def present(self) -> tuple[AuditLink, ...]:
        """Return only the links that were actually found."""

        return tuple(link for link in self.links if link.state == "present")


class GatewayAudit:
    """Follow exact ids across every subsystem without copying their contents."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        scope: ProfileScope,
        messaging: MessagingStore | None = None,
        notifications: NotificationStore | None = None,
        gateway: GatewayStore | None = None,
        executions: ExecutionStore | None = None,
        jobs: JobRunStore | None = None,
        authority: AuthorityStore | None = None,
    ) -> None:
        self.settings = settings
        self.profile_scope = scope
        self.messaging = messaging or MessagingStore(settings)
        self.notifications = notifications or NotificationStore(settings)
        self.gateway = gateway or GatewayStore(settings)
        self.executions = executions or ExecutionStore(settings)
        self.jobs = jobs or JobRunStore(settings)
        self.authority = authority or AuthorityStore(settings)

    async def trace(self, correlation_id: str, *, now: datetime | None = None) -> AuditChain:
        """Project the full chain reachable from one canonical id."""

        moment = now or datetime.now(UTC)
        query = correlation_id.strip()
        if not query:
            raise ValueError("audit correlation id cannot be empty")
        await self._initialize()
        kind = _classify(query)
        links: list[AuditLink] = []
        if kind == "inbound_message":
            links.extend(await self._from_inbound(query))
        elif kind == "conversation":
            links.extend(await self._from_conversation(query))
        elif kind == "execution_request":
            links.extend(await self._from_execution(query))
        elif kind == "execution_draft":
            links.extend(await self._from_draft(query))
        elif kind == "confirmation":
            links.extend(await self._from_confirmation(query))
        elif kind == "execution_contract":
            links.extend(await self._from_contract(query))
        elif kind == "delegation_grant":
            links.extend(await self._from_grant(query))
        elif kind == "notification":
            links.extend(await self._from_notification(query))
        else:
            links.append(
                AuditLink(
                    kind="inbound_message",
                    state="missing",
                    detail=(
                        f"'{query}' is not a recognised canonical id; audit accepts an "
                        "inbound, conversation, draft, confirmation, contract, execution, "
                        "grant, or outbox id"
                    ),
                )
            )
        return AuditChain(
            generated_at=moment,
            query=query,
            resolved_kind=kind,
            links=tuple(links),
        )

    async def _initialize(self) -> None:
        await self.messaging.initialize()
        await self.notifications.initialize()
        await self.gateway.initialize()
        await self.executions.initialize()
        await self.jobs.initialize()
        await self.authority.initialize()

    async def _from_inbound(self, message_id: str) -> list[AuditLink]:
        links: list[AuditLink] = []
        message = None
        with suppress(Exception):
            message = await self.messaging.get_inbox(message_id)
        if message is None:
            return [
                AuditLink(
                    kind="inbound_message",
                    state="missing",
                    id=message_id,
                    detail="no inbound message with this id is stored",
                )
            ]
        links.append(
            AuditLink(
                kind="inbound_message",
                state="present",
                id=message.id,
                status=message.status,
                at=message.received_at,
                detail=(
                    f"{message.transport}/{message.account} from sender {message.sender_id} "
                    f"to {message.destination_id}"
                ),
            )
        )
        result = await self.gateway.result_for_message(
            message.id,
            scope=self.profile_scope,
        )
        if result is None:
            links.append(
                AuditLink(
                    kind="foreground_turn",
                    state="missing",
                    detail="this message was never claimed by a foreground turn",
                )
            )
            return links
        links.extend(await self._conversation_link(result.conversation_id))
        links.append(
            AuditLink(
                kind="foreground_turn",
                state="present",
                id=result.message_id,
                status=result.status,
                at=result.finished_at or result.started_at,
                detail=(
                    f"session {result.session_id}"
                    + (f"; error: {result.error}" if result.error else "")
                ),
            )
        )
        links.extend(await self._draft_links_for_message(message.id))
        if result.response_outbox_id is not None:
            links.extend(await self._notification_links(result.response_outbox_id))
        else:
            links.append(
                AuditLink(
                    kind="notification",
                    state="missing",
                    detail="the turn produced no outbound notification",
                )
            )
        links.extend(await self._execution_links_for_conversation(result.conversation_id))
        return links

    async def _from_conversation(self, conversation_id: str) -> list[AuditLink]:
        links = await self._conversation_link(conversation_id)
        if links and links[0].state != "present":
            return links
        results = await self.gateway.results(
            conversation_id,
            scope=self.profile_scope,
            limit=50,
        )
        if not results:
            links.append(
                AuditLink(
                    kind="foreground_turn",
                    state="missing",
                    detail="this conversation has processed no message",
                )
            )
            return links
        for result in results:
            links.append(
                AuditLink(
                    kind="foreground_turn",
                    state="present",
                    id=result.message_id,
                    status=result.status,
                    at=result.finished_at or result.started_at,
                    detail=f"session {result.session_id}",
                )
            )
        links.extend(await self._execution_links_for_conversation(conversation_id))
        return links

    async def _from_execution(self, request_id: str) -> list[AuditLink]:
        request = None
        with suppress(Exception):
            request = await self.executions.get(request_id, scope=self.profile_scope)
        if request is None:
            return [
                AuditLink(
                    kind="execution_request",
                    state="missing",
                    id=request_id,
                    detail="no execution request with this id is stored",
                )
            ]
        links: list[AuditLink] = []
        if request.task_id is not None:
            links.extend(await self._task_link(request.task_id, request.profile_scope))
        if request.contract_id is not None:
            draft = await self.executions.find_draft_for_contract(
                request.contract_id,
                scope=self.profile_scope,
            )
            if draft is not None:
                links.extend(self._draft_evidence_links(draft))
            links.extend(await self._contract_link(request.contract_id))
        if request.grant_id is not None:
            links.extend(await self._grant_link(request.grant_id))
        execution_source = request.contract_id or request.named_job or "unknown"
        links.append(
            AuditLink(
                kind="execution_request",
                state="present",
                id=request.id,
                status=request.status,
                at=request.created_at,
                detail=(
                    f"{request.kind} via {execution_source}"
                    + (f"; error: {request.error}" if request.error else "")
                ),
            )
        )
        if request.run_id is None:
            links.append(
                AuditLink(
                    kind="job_run",
                    state="missing",
                    detail="this request never started a run",
                )
            )
            return links
        links.extend(await self._run_links(request.run_id))
        return links

    async def _from_grant(self, grant_id: str) -> list[AuditLink]:
        links = await self._grant_link(grant_id)
        grant = None
        with suppress(Exception):
            grant = await self.authority.get(grant_id, scope=self.profile_scope)
        if grant is None:
            return links
        links.extend(await self._task_link(grant.task_id, grant.profile_scope))
        if grant.execution_request_id is None:
            links.append(
                AuditLink(
                    kind="execution_request",
                    state="missing",
                    detail="this grant was never attached to an execution request",
                )
            )
            return links
        links.extend(await self._from_execution(grant.execution_request_id))
        return links

    async def _from_draft(self, draft_id: str) -> list[AuditLink]:
        try:
            draft = await self.executions.get_draft(draft_id, scope=self.profile_scope)
        except Exception:
            return [
                AuditLink(
                    kind="execution_draft",
                    state="missing",
                    id=draft_id,
                    detail="no execution draft with this id is stored",
                )
            ]
        links = self._draft_evidence_links(draft)
        if draft.contract_id is not None:
            links.extend(await self._contract_link(draft.contract_id))
        if draft.request_id is not None:
            links.extend(await self._from_execution(draft.request_id))
        return links

    async def _from_confirmation(self, confirmation_id: str) -> list[AuditLink]:
        try:
            confirmation = await self.executions.get_confirmation(
                confirmation_id,
                scope=self.profile_scope,
            )
        except Exception:
            return [
                AuditLink(
                    kind="confirmation",
                    state="missing",
                    id=confirmation_id,
                    detail="no execution confirmation with this id is stored",
                )
            ]
        return await self._from_draft(confirmation.draft_id)

    async def _from_contract(self, contract_id: str) -> list[AuditLink]:
        links = await self._contract_link(contract_id)
        try:
            contract = await self.executions.get_contract(
                contract_id,
                scope=self.profile_scope,
            )
        except Exception:
            return links
        draft = await self.executions.find_draft_for_contract(
            contract.id,
            scope=self.profile_scope,
        )
        if draft is not None:
            links = [*self._draft_evidence_links(draft), *links]
        request = await self.executions.request_for_contract(
            contract.id,
            scope=self.profile_scope,
        )
        if request is not None:
            links.extend(await self._from_execution(request.id))
        return links

    async def _from_notification(self, outbox_id: str) -> list[AuditLink]:
        return await self._notification_links(outbox_id)

    async def _conversation_link(self, conversation_id: str) -> list[AuditLink]:
        conversation = None
        with suppress(Exception):
            conversation = await self.gateway.get(
                conversation_id,
                scope=self.profile_scope,
            )
        if conversation is None:
            return [
                AuditLink(
                    kind="conversation",
                    state="missing",
                    id=conversation_id,
                    detail="no conversation with this id is stored",
                )
            ]
        return [
            AuditLink(
                kind="conversation",
                state="present",
                id=conversation.id,
                status=conversation.status,
                at=conversation.updated_at,
                detail=(
                    f"route {conversation.route_name} with profiles "
                    f"{', '.join(conversation.profile_scope.profiles)}, "
                    f"revision {conversation.revision}"
                ),
            )
        ]

    async def _draft_links_for_message(self, message_id: str) -> list[AuditLink]:
        links: list[AuditLink] = []
        for draft in await self.executions.find_drafts_by_message(
            message_id,
            scope=self.profile_scope,
            limit=20,
        ):
            links.extend(self._draft_evidence_links(draft))
            if draft.contract_id is not None:
                links.extend(await self._contract_link(draft.contract_id))
        return links

    def _draft_evidence_links(self, draft: ExecutionDraft) -> list[AuditLink]:
        links = [
            AuditLink(
                kind="execution_draft",
                state="present",
                id=draft.id,
                status=draft.status,
                at=draft.updated_at,
                detail=(
                    f"revision {draft.revision}; task {draft.task_id}; "
                    f"capabilities {', '.join(draft.requested_capabilities)}"
                ),
            )
        ]
        links.extend(
            AuditLink(
                kind="guardrail",
                state="present",
                id=f"{draft.id}:{field.capability_id}:{field.field}",
                status="collected",
                at=draft.updated_at,
                detail=(
                    f"{field.schema_id}/v{field.schema_version} field {field.field}; "
                    f"source {field.source_message_id}"
                ),
            )
            for field in draft.collected_guardrail_fields
        )
        links.extend(
            AuditLink(
                kind="guardrail",
                state="present",
                id=guardrail.digest,
                status=f"{guardrail.schema_id}/v{guardrail.schema_version}",
                at=draft.updated_at,
                detail=(
                    f"{guardrail.capability_id}; sources {', '.join(guardrail.source_message_ids)}"
                ),
            )
            for guardrail in draft.guardrails
        )
        if draft.confirmation is not None:
            links.append(
                AuditLink(
                    kind="confirmation",
                    state="present",
                    id=draft.confirmation.id,
                    status="confirmed",
                    at=draft.confirmation.confirmed_at,
                    detail=(
                        f"draft revision {draft.confirmation.draft_revision}; source "
                        f"{draft.confirmation.source_message_id}; expires "
                        f"{draft.confirmation.expires_at.isoformat()}"
                    ),
                )
            )
        return links

    async def _contract_link(self, contract_id: str) -> list[AuditLink]:
        try:
            contract = await self.executions.get_contract(
                contract_id,
                scope=self.profile_scope,
            )
        except Exception:
            return [
                AuditLink(
                    kind="execution_contract",
                    state="missing",
                    id=contract_id,
                    detail="no immutable execution contract with this id is stored",
                )
            ]
        return [
            AuditLink(
                kind="execution_contract",
                state="present",
                id=contract.id,
                status="compiled",
                at=contract.created_at,
                detail=(
                    f"digest {contract.digest}; route {contract.route_name}; "
                    f"{len(contract.tools)} tool(s), {len(contract.skills)} skill(s)"
                ),
            )
        ]

    async def _task_link(self, task_id: str, scope: ProfileScope) -> list[AuditLink]:
        task = None
        with suppress(Exception):
            store = await ScopedDurableTaskStore.create(self.settings, scope=scope)
            task = await store.get_task(task_id)
        if task is None:
            return [
                AuditLink(
                    kind="durable_task",
                    state="missing",
                    id=task_id,
                    detail="no in-scope durable task with this id is stored",
                )
            ]
        return [
            AuditLink(
                kind="durable_task",
                state="present",
                id=task.id,
                status=task.status,
                at=task.updated_at,
                detail=f"{task.title} (revision {task.revision})",
            )
        ]

    async def _grant_link(self, grant_id: str) -> list[AuditLink]:
        grant = None
        with suppress(Exception):
            grant = await self.authority.get(grant_id, scope=self.profile_scope)
        if grant is None:
            return [
                AuditLink(
                    kind="delegation_grant",
                    state="missing",
                    id=grant_id,
                    detail="no delegation grant with this id is stored",
                )
            ]
        capabilities = ", ".join(scope.capability for scope in grant.scopes)
        return [
            AuditLink(
                kind="delegation_grant",
                state="present",
                id=grant.id,
                status=grant.status,
                at=grant.issued_at,
                detail=f"{capabilities} for task {grant.task_id}, expires {grant.expires_at}",
            )
        ]

    async def _run_links(self, run_id: str) -> list[AuditLink]:
        run = None
        with suppress(Exception):
            run = await self.jobs.get(run_id, scope=self.profile_scope)
        if run is None:
            return [
                AuditLink(
                    kind="job_run",
                    state="missing",
                    id=run_id,
                    detail="no job run with this id is stored",
                )
            ]
        links = [
            AuditLink(
                kind="job_run",
                state="present",
                id=run.id,
                status=run.outcome or "running",
                at=run.finished_at or run.started_at,
                # The transcript path is a reference. Its body is never copied here.
                detail=(
                    f"{run.iterations} iteration(s), {run.effect_calls} effect call(s); "
                    f"transcript: {run.transcript_path or 'none'}"
                ),
            )
        ]
        actions = [
            action
            for action in await self.jobs.list_actions(
                scope=self.profile_scope,
                limit=500,
            )
            if action.run_id == run_id
        ]
        if not actions:
            links.append(
                AuditLink(
                    kind="external_action",
                    state="missing",
                    detail="this run reserved no external effect",
                )
            )
        for action in actions:
            links.append(
                AuditLink(
                    kind="external_action",
                    state="present",
                    id=action.id,
                    status=action.status,
                    at=action.created_at,
                    detail=(
                        f"{action.operation} on {action.target}; "
                        f"reference: {action.provider_reference or 'none'}"
                    ),
                )
            )
        return links

    async def _notification_links(self, outbox_id: str) -> list[AuditLink]:
        record = None
        with suppress(Exception):
            record = await self.notifications.get_by_outbox(
                outbox_id,
                scope=self.profile_scope,
            )
        if record is None:
            return [
                AuditLink(
                    kind="notification",
                    state="missing",
                    id=outbox_id,
                    detail="no notification with this outbox id is stored",
                )
            ]
        links = [
            AuditLink(
                kind="notification",
                state="present",
                id=record.outbox.id,
                status=record.outbox.status,
                at=record.outbox.delivered_at or record.outbox.updated_at,
                detail=(
                    f"route {record.request.route}, {record.outbox.attempt_count} attempt(s)"
                    + (f"; error: {record.outbox.error}" if record.outbox.error else "")
                ),
            )
        ]
        parts = await self.messaging.delivery_parts(outbox_id)
        if not parts:
            links.append(
                AuditLink(
                    kind="delivery_receipt",
                    state="missing",
                    detail="no transport part was ever prepared for this notification",
                )
            )
            return links
        for part in parts:
            links.append(
                AuditLink(
                    kind="delivery_receipt",
                    state="present",
                    id=part.message.id,
                    status=part.status,
                    at=part.updated_at,
                    detail=(
                        f"part {part.message.part_number}/{part.message.part_count}; "
                        f"platform id: {part.platform_message_id or 'none'}"
                    ),
                )
            )
        links.extend(await self._reply_links(parts))
        return links

    async def _reply_links(self, parts: list) -> list[AuditLink]:  # type: ignore[type-arg]
        for part in parts:
            if part.platform_message_id is None:
                continue
            matches = [
                message
                for message in await self.messaging.list_inbox(limit=1_000)
                if message.reply_to_platform_message_id == part.platform_message_id
                and message.destination_id == part.message.destination_id
            ]
            for message in matches:
                return [
                    AuditLink(
                        kind="user_reply",
                        state="present",
                        id=message.id,
                        status=message.status,
                        at=message.received_at,
                        detail=f"reply to delivered part {part.platform_message_id}",
                    )
                ]
        return [
            AuditLink(
                kind="user_reply",
                state="missing",
                detail="no inbound message replies to a delivered part of this notification",
            )
        ]

    async def _execution_links_for_conversation(self, conversation_id: str) -> list[AuditLink]:
        records = await self.notifications.list(scope=self.profile_scope, limit=200)
        linked: list[str] = []
        for record in records:
            if not any(
                ref.kind == "conversation" and ref.id == conversation_id
                for ref in record.request.correlations
            ):
                continue
            for ref in record.request.correlations:
                if ref.kind == "execution_request" and ref.id not in linked:
                    linked.append(ref.id)
        links: list[AuditLink] = []
        for request_id in linked[:10]:
            links.extend(await self._from_execution(request_id))
        return links


def _classify(value: str) -> AuditLinkKind | None:
    """Recognise a canonical id by its documented prefix. Never guesses."""

    prefixes: tuple[tuple[str, AuditLinkKind], ...] = (
        ("inbound_", "inbound_message"),
        ("conversation_", "conversation"),
        ("draft_", "execution_draft"),
        ("confirmation_", "confirmation"),
        ("contract_", "execution_contract"),
        ("execution_", "execution_request"),
        ("grant_", "delegation_grant"),
        ("outbox_", "notification"),
    )
    for prefix, kind in prefixes:
        if value.startswith(prefix):
            return kind
    return None
