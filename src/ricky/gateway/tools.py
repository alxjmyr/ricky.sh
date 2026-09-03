"""Source-bound foreground execution controls."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field, RootModel

from ricky.authority.compiler import ContractAuthorityCompiler, build_grant_source
from ricky.capabilities import AuthenticatedSource
from ricky.executions.compiler import ExecutionContractCompiler
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.executions.drafts import AdHocDelegationCommand
from ricky.executions.types import ExecutionRequest, ExecutionStatus
from ricky.gateway.capability_use import PrepareCapabilityUseTool
from ricky.gateway.types import Conversation
from ricky.messaging.types import InboundMessage
from ricky.project_scope import ProjectScope
from ricky.tools import Tool, ToolContext, ToolResult, UserInteractionRequest


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StartNamedJobParams(_Params):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    task_id: str | None = None
    task_revision: int | None = Field(default=None, ge=1)


class DelegateTaskParams(RootModel[AdHocDelegationCommand]):
    """One top-level discriminated delegation command.

    Keeping the action at the tool-argument root avoids a nested union object
    that provider adapters can incorrectly serialize as a JSON string.
    """

    model_config = ConfigDict(strict=True)


class ExecutionIdParams(_Params):
    request_id: str = Field(pattern=r"^execution_[0-9a-f]{32}$")


class ListExecutionRequestsParams(_Params):
    status: ExecutionStatus | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ExecutionQueuedResult(_Params):
    request_id: str
    kind: str
    status: str
    task_id: str | None = None


class DelegationReviewResult(_Params):
    status: str
    draft_id: str
    draft_revision: int
    questions: list[str] = Field(default_factory=list)
    confirmation_summary: str | None = None
    contract_id: str | None = None
    request_id: str | None = None
    grant_id: str | None = None


class _GatewayExecutionTool:
    risk: ClassVar[str]
    capability_id = "builtin.automation.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(
        self,
        dispatcher: ExecutionDispatcher,
        *,
        conversation: Conversation,
        inbound: InboundMessage,
        project_scope: ProjectScope | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.conversation = conversation
        self.inbound = inbound
        self.project_scope = project_scope

    @property
    def route(self) -> str:
        return f"conversation:{self.conversation.id}"

    def key(self, operation: str) -> str:
        value = f"{self.conversation.id}::{self.inbound.id}::{operation}"
        return hashlib.sha256(value.encode()).hexdigest()

    def owned(self, request: ExecutionRequest) -> bool:
        return request.source_conversation_id == self.conversation.id


class StartNamedJobTool(_GatewayExecutionTool):
    name = "start_named_job"
    description = "Queue one exact named job; route and source are gateway-bound."
    Params = StartNamedJobParams
    Result = ExecutionQueuedResult
    risk = "mutating"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = StartNamedJobParams.model_validate(params)
        request = await self.dispatcher.start_named_job(
            args.name,
            notification_route=self.route,
            request_key=self.key(self.name),
            profile_scope=ctx.session.profile_scope,
            task_id=args.task_id,
            task_revision=args.task_revision,
            source_conversation_id=self.conversation.id,
            source_message_id=self.inbound.id,
            project_scope=self.project_scope,
        )
        return _queued(request)


class DelegateTaskTool(_GatewayExecutionTool):
    """Unified source-bound draft, confirmation, compile, and queue surface."""

    name = "delegate_task"
    description = (
        "Start capability-scoped ad hoc background work, supply structurally typed guardrail "
        "fields to its durable draft, confirm its exact summary, or cancel a pending review. "
        "Use one top-level discriminated action: start, supply_guardrails, confirm, or cancel. "
        "Continuations never repeat task or capability data. Code binds source identity and "
        "route, compiles an immutable execution contract, and queues once. Never claim queued "
        "work is complete."
    )
    Params = DelegateTaskParams
    Result = DelegationReviewResult
    risk = "mutating"

    def __init__(
        self,
        dispatcher: ExecutionDispatcher,
        compiler: ExecutionContractCompiler,
        contract_authority: ContractAuthorityCompiler | None = None,
        *,
        conversation: Conversation,
        inbound: InboundMessage,
    ) -> None:
        super().__init__(dispatcher, conversation=conversation, inbound=inbound)
        self.compiler = compiler
        self.contract_authority = contract_authority

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        command = DelegateTaskParams.model_validate(params).root
        text = self.inbound.text[: ctx.settings.executions.source_snapshot_chars]
        source = AuthenticatedSource(
            principal_id=(
                f"{self.inbound.transport}:{self.inbound.account}:{self.inbound.sender_id}"
            ),
            conversation_id=self.conversation.id,
            message_id=self.inbound.id,
            text_digest=hashlib.sha256(self.inbound.text.encode("utf-8")).hexdigest(),
            text_snapshot=text,
            received_at=self.inbound.received_at,
        )
        draft = await self.compiler.review(command, source=source)
        if draft.status == "cancelled":
            result = DelegationReviewResult(
                status=draft.status,
                draft_id=draft.id,
                draft_revision=draft.revision,
            )
            return ToolResult(
                content=(
                    f"Execution draft {draft.id} was cancelled. No execution contract or "
                    "background request was created. The linked durable task is separate "
                    "and was not cancelled by this action."
                ),
                data=result.model_dump(mode="json"),
            )
        if draft.status == "collecting_guardrails":
            result = DelegationReviewResult(
                status=draft.status,
                draft_id=draft.id,
                draft_revision=draft.revision,
                questions=list(draft.pending_questions),
            )
            prompt = "\n".join(f"- {item}" for item in draft.pending_questions)
            return ToolResult(
                content=(
                    f"Execution draft {draft.id} at revision {draft.revision} needs "
                    "authenticated user input. "
                    "The turn will stop after delivering the exact questions."
                ),
                data=result.model_dump(mode="json"),
                user_interaction=UserInteractionRequest(
                    kind="guardrail_input",
                    correlation_id=f"{draft.id}:{draft.revision}",
                    prompt=prompt,
                ),
            )
        if draft.status == "awaiting_confirmation":
            result = DelegationReviewResult(
                status=draft.status,
                draft_id=draft.id,
                draft_revision=draft.revision,
                confirmation_summary=draft.confirmation_summary,
            )
            assert draft.confirmation_summary is not None
            return ToolResult(
                content=(
                    f"Execution draft {draft.id} at revision {draft.revision} requires "
                    "exact confirmation. "
                    "The turn will stop after delivering the immutable summary."
                ),
                data=result.model_dump(mode="json"),
                user_interaction=UserInteractionRequest(
                    kind="confirmation",
                    correlation_id=f"{draft.id}:{draft.revision}",
                    prompt=(
                        f"{draft.confirmation_summary}\n\n"
                        "Reply Yes to approve this exact task-scoped execution."
                    ),
                ),
            )
        if draft.status != "ready":
            return ToolResult(
                content=f"execution draft cannot be queued from {draft.status}",
                is_error=True,
            )

        already_compiled = draft.contract_id is not None
        contract = await self.compiler.compile(draft)
        draft = await self.compiler.store.get_draft(draft.id, scope=self.conversation.profile_scope)
        request = await self.compiler.store.request_for_contract(
            contract.id, scope=self.conversation.profile_scope
        )
        grant_id = request.grant_id if request is not None else None
        if request is None:
            if already_compiled:
                return ToolResult(
                    content=(
                        f"Execution contract {contract.id} was committed without a request. "
                        "It requires operator review and will not be submitted again implicitly."
                    ),
                    is_error=True,
                )
            grant = None
            if any(item.authority_capability is not None for item in contract.capabilities):
                if self.contract_authority is None:
                    return ToolResult(
                        content="effectful execution contract has no authority compiler",
                        is_error=True,
                    )
                grant = await self.contract_authority.compile(
                    contract,
                    source=build_grant_source(
                        self.inbound,
                        conversation_id=self.conversation.id,
                        snapshot_chars=ctx.settings.authority.source_snapshot_chars,
                    ),
                )
            try:
                request = await self.dispatcher.create_contract_execution_request(
                    contract,
                    request_key=self.key(f"delegate:{draft.id}:{contract.digest}"),
                    grant_id=grant.id if grant is not None else None,
                )
                grant_id = grant.id if grant is not None else None
            except Exception:
                if grant is not None:
                    await self.dispatcher.revoke_grant(
                        grant.id,
                        scope=self.conversation.profile_scope,
                        actor="delegate_task",
                        reason="contract execution could not be queued",
                    )
                raise
        queued = draft.model_copy(
            update={
                "status": "queued",
                "revision": draft.revision + 1,
                "contract_id": contract.id,
                "request_id": request.id,
                "updated_at": datetime.now(UTC),
            }
        )
        await self.compiler.store.update_draft(
            queued,
            expected_revision=draft.revision,
            kind="queued",
            summary=f"Queued execution request {request.id}",
            scope=self.conversation.profile_scope,
        )
        result = DelegationReviewResult(
            status="queued",
            draft_id=queued.id,
            draft_revision=queued.revision,
            contract_id=contract.id,
            request_id=request.id,
            grant_id=grant_id,
        )
        return ToolResult(
            content=(
                f"Execution queued: {request.id} under immutable contract {contract.id}. "
                "Report it as queued, not completed."
            ),
            data=result.model_dump(mode="json"),
        )


class CancelExecutionRequestTool(_GatewayExecutionTool):
    name = "cancel_execution_request"
    description = "Cancel an execution linked to this conversation."
    Params = ExecutionIdParams
    risk = "mutating"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = ExecutionIdParams.model_validate(params)
        current = await self.dispatcher.read_execution_request(
            args.request_id, scope=self.conversation.profile_scope
        )
        if not self.owned(current):
            return ToolResult(
                content="execution is not controlled by this conversation", is_error=True
            )
        request = await self.dispatcher.cancel_execution_request(
            args.request_id, scope=self.conversation.profile_scope
        )
        return ToolResult(content=f"{request.id}: {request.status}")


class ReadExecutionRequestTool(_GatewayExecutionTool):
    name = "read_execution_request"
    description = "Read an execution linked to this conversation."
    Params = ExecutionIdParams
    risk = "read_only"
    capability_id = "builtin.automation.read"
    effect_kind = "none"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = ExecutionIdParams.model_validate(params)
        request = await self.dispatcher.read_execution_request(
            args.request_id, scope=self.conversation.profile_scope
        )
        if not self.owned(request):
            return ToolResult(content="execution is not linked to this conversation", is_error=True)
        return ToolResult(content=request.model_dump_json(indent=2))


class ListExecutionRequestsTool(_GatewayExecutionTool):
    name = "list_execution_requests"
    description = "List recent executions linked to this conversation."
    Params = ListExecutionRequestsParams
    risk = "read_only"
    capability_id = "builtin.automation.read"
    effect_kind = "none"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = ListExecutionRequestsParams.model_validate(params)
        requests = await self.dispatcher.list_execution_requests(
            scope=self.conversation.profile_scope,
            status=args.status,
            limit=min(100, args.limit * 5),
        )
        owned = [item for item in requests if self.owned(item)][: args.limit]
        return ToolResult(
            content="\n".join(
                f"{item.id} {item.kind} {item.status} task={item.task_id or '-'}" for item in owned
            )
            or "No linked execution requests found."
        )


def gateway_execution_tools(
    dispatcher: ExecutionDispatcher,
    *,
    conversation: Conversation,
    inbound: InboundMessage,
    compiler: ExecutionContractCompiler | None = None,
    contract_authority: ContractAuthorityCompiler | None = None,
    capability_ids: set[str] | None = None,
    project_scope: ProjectScope | None = None,
) -> list[Tool]:
    candidates: list[tuple[str, Tool]] = [
        (
            "builtin.automation.mutate",
            cast(
                Tool,
                StartNamedJobTool(
                    dispatcher,
                    conversation=conversation,
                    inbound=inbound,
                    project_scope=project_scope,
                ),
            ),
        ),
        (
            "builtin.automation.mutate",
            cast(
                Tool,
                CancelExecutionRequestTool(dispatcher, conversation=conversation, inbound=inbound),
            ),
        ),
        (
            "builtin.automation.read",
            cast(
                Tool,
                ReadExecutionRequestTool(dispatcher, conversation=conversation, inbound=inbound),
            ),
        ),
        (
            "builtin.automation.read",
            cast(
                Tool,
                ListExecutionRequestsTool(dispatcher, conversation=conversation, inbound=inbound),
            ),
        ),
    ]
    if compiler is not None:
        candidates.append(
            (
                "builtin.automation.mutate",
                cast(
                    Tool,
                    DelegateTaskTool(
                        dispatcher,
                        compiler,
                        contract_authority,
                        conversation=conversation,
                        inbound=inbound,
                    ),
                ),
            )
        )
    selected = capability_ids or set()
    return [tool for capability, tool in candidates if capability in selected]


def gateway_control_descriptors() -> tuple[Tool, ...]:
    """Provider/store-free schema descriptors for source-bound gateway controls."""

    from ricky.authority.tools import ListDelegationsTool, RevokeDelegationTool

    descriptor_types = (
        PrepareCapabilityUseTool,
        StartNamedJobTool,
        DelegateTaskTool,
        CancelExecutionRequestTool,
        ReadExecutionRequestTool,
        ListExecutionRequestsTool,
        RevokeDelegationTool,
        ListDelegationsTool,
    )
    return tuple(cast(Tool, descriptor.__new__(descriptor)) for descriptor in descriptor_types)


def gateway_capability_inventory_tools(
    base_tools: Iterable[Tool],
    *interface_tool_groups: Iterable[Tool],
) -> tuple[Tool, ...]:
    """Add gateway-only controls without replacing neutral callable contracts."""

    by_name = {tool.name: tool for tool in base_tools}
    for group in interface_tool_groups:
        for tool in group:
            by_name.setdefault(tool.name, tool)
    return tuple(by_name.values())


def _queued(request: ExecutionRequest) -> ToolResult:
    data = ExecutionQueuedResult(
        request_id=request.id,
        kind=request.kind,
        status=request.status,
        task_id=request.task_id,
    )
    return ToolResult(
        content=(
            f"Execution queued: {request.id}; task: {request.task_id or '-'}. "
            "Report it as queued, not completed."
        ),
        data=data.model_dump(mode="json"),
    )
