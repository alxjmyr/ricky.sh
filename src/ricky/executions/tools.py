"""Agent-facing control-plane tools for durable execution requests."""

from __future__ import annotations

import hashlib
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field

from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.executions.types import ExecutionStatus
from ricky.permissions.types import GrantScope
from ricky.tools.base import Risk, Tool, ToolContext, ToolResult


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StartNamedJobParams(_Params):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    route: str = Field(min_length=1, max_length=200)
    occurrence_key: str = Field(min_length=1, max_length=500)
    task_id: str | None = None
    task_revision: int | None = Field(default=None, ge=1)


class ExecutionIdParams(_Params):
    request_id: str = Field(pattern=r"^execution_[0-9a-f]{32}$")


class ListExecutionRequestsParams(_Params):
    status: ExecutionStatus | None = None
    limit: int = Field(default=20, ge=1, le=100)


class _ExecutionTool:
    risk: ClassVar[Risk]
    capability_id = "builtin.automation.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, dispatcher: ExecutionDispatcher, *, allowed_routes: set[str]) -> None:
        self.dispatcher = dispatcher
        self.allowed_routes = frozenset(allowed_routes)

    def _route(self, route: str) -> ToolResult | None:
        if route not in self.allowed_routes:
            return ToolResult(content=f"execution route is not allowed: {route}", is_error=True)
        return None

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        del ctx
        route = args.get("route")
        return GrantScope(
            params_equal={"route": route},
            label=f"allow durable execution requests on route {route!r} for this session",
            allow_unconstrained=False,
        )


class StartNamedJobTool(_ExecutionTool):
    name = "start_named_job"
    description = "Queue one exact existing named job and return immediately."
    Params = StartNamedJobParams
    risk = "mutating"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = StartNamedJobParams.model_validate(params)
        denied = self._route(args.route)
        if denied is not None:
            return denied
        request = await self.dispatcher.start_named_job(
            args.name,
            notification_route=args.route,
            request_key=_key(ctx.session.id, self.name, args.occurrence_key),
            profile_scope=ctx.session.profile_scope,
            task_id=args.task_id,
            task_revision=args.task_revision,
            source_conversation_id=ctx.session.id,
        )
        return ToolResult(content=f"execution queued: {request.id}")


class CancelExecutionRequestTool(_ExecutionTool):
    name = "cancel_execution_request"
    description = "Cancel queued or active durable background execution."
    Params = ExecutionIdParams
    risk = "mutating"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ExecutionIdParams.model_validate(params)
        request = await self.dispatcher.cancel_execution_request(
            args.request_id, scope=ctx.session.profile_scope
        )
        return ToolResult(content=f"{request.id}: {request.status}")


class ReadExecutionRequestTool(_ExecutionTool):
    name = "read_execution_request"
    description = "Read one durable execution request and its current state."
    Params = ExecutionIdParams
    risk = "read_only"
    capability_id = "builtin.automation.read"
    effect_kind = "none"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ExecutionIdParams.model_validate(params)
        request = await self.dispatcher.read_execution_request(
            args.request_id, scope=ctx.session.profile_scope
        )
        return ToolResult(content=request.model_dump_json(indent=2))


class ListExecutionRequestsTool(_ExecutionTool):
    name = "list_execution_requests"
    description = "List recent durable execution requests."
    Params = ListExecutionRequestsParams
    risk = "read_only"
    capability_id = "builtin.automation.read"
    effect_kind = "none"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ListExecutionRequestsParams.model_validate(params)
        requests = await self.dispatcher.list_execution_requests(
            scope=ctx.session.profile_scope,
            status=args.status,
            limit=args.limit,
        )
        return ToolResult(
            content="\n".join(
                f"{item.id} {item.kind} {item.status} run={item.run_id or '-'}" for item in requests
            )
            or "No execution requests found."
        )


def execution_tools(dispatcher: ExecutionDispatcher, *, allowed_routes: set[str]) -> list[Tool]:
    return [
        cast(Tool, StartNamedJobTool(dispatcher, allowed_routes=allowed_routes)),
        cast(Tool, CancelExecutionRequestTool(dispatcher, allowed_routes=allowed_routes)),
        cast(Tool, ReadExecutionRequestTool(dispatcher, allowed_routes=allowed_routes)),
        cast(Tool, ListExecutionRequestsTool(dispatcher, allowed_routes=allowed_routes)),
    ]


def _key(session_id: str, tool_name: str, occurrence: str) -> str:
    return hashlib.sha256(f"{session_id}\0{tool_name}\0{occurrence}".encode()).hexdigest()
