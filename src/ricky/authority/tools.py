"""Source-bound foreground controls for inspecting and revoking grants."""

from __future__ import annotations

from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field

from ricky.authority.store import AuthorityStore, AuthorityStoreError
from ricky.authority.types import DelegationGrant
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.tools import Tool, ToolContext, ToolResult


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GrantIdParams(_Params):
    grant_id: str = Field(pattern=r"^grant_[0-9a-f]{32}$")
    reason: str = Field(min_length=1, max_length=500)


class RevokeDelegationTool:
    """Revoke active delegated authority on direct user instruction."""

    name: ClassVar[str] = "revoke_delegation"
    description: ClassVar[str] = (
        "Revoke an active delegation grant issued from this conversation. Revocation "
        "prevents a future tool call; it cannot undo an effect that already happened."
    )
    Params: ClassVar[type[BaseModel]] = GrantIdParams
    risk: ClassVar[str] = "mutating"
    capability_id = "builtin.automation.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(
        self,
        store: AuthorityStore,
        dispatcher: ExecutionDispatcher,
        *,
        conversation_id: str,
    ) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self.conversation_id = conversation_id

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GrantIdParams.model_validate(params)
        await self.store.initialize()
        try:
            grant = await self.store.get(args.grant_id, scope=ctx.session.profile_scope)
        except AuthorityStoreError as exc:
            return ToolResult(content=str(exc), is_error=True)
        if grant.source.conversation_id != self.conversation_id:
            return ToolResult(
                content="that grant is not controlled by this conversation", is_error=True
            )
        if grant.status != "active":
            return ToolResult(content=f"grant {grant.id} is already {grant.status}")
        await self.dispatcher.revoke_grant(
            grant.id,
            scope=ctx.session.profile_scope,
            actor=f"conversation:{self.conversation_id}",
            reason=args.reason,
        )
        return ToolResult(
            content=(
                f"Revoked {grant.id}. Future delegated calls are denied; any already "
                "confirmed effect is unchanged."
            )
        )


class ListDelegationsTool:
    """List delegated authority issued from this conversation."""

    name: ClassVar[str] = "list_delegations"
    description: ClassVar[str] = "List delegation grants issued from this conversation."
    Params: ClassVar[type[BaseModel]] = _Params
    risk: ClassVar[str] = "read_only"
    capability_id = "builtin.automation.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, store: AuthorityStore, *, conversation_id: str) -> None:
        self.store = store
        self.conversation_id = conversation_id

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params
        await self.store.initialize()
        grants = [
            grant
            for grant in await self.store.list(scope=ctx.session.profile_scope, limit=100)
            if grant.source.conversation_id == self.conversation_id
        ]
        if not grants:
            return ToolResult(content="No delegation grants belong to this conversation.")
        return ToolResult(content="\n".join(_line(grant) for grant in grants[:20]))


def _line(grant: DelegationGrant) -> str:
    return (
        f"{grant.id} {grant.status} task={grant.task_id} "
        f"expires={grant.expires_at.isoformat()} :: {grant.summary[:200]}"
    )


def delegation_management_tools(
    dispatcher: ExecutionDispatcher,
    store: AuthorityStore,
    *,
    conversation_id: str,
) -> list[Tool]:
    """Source-bound list/revoke controls shared by legacy and contract grants."""

    return [
        cast(Tool, RevokeDelegationTool(store, dispatcher, conversation_id=conversation_id)),
        cast(Tool, ListDelegationsTool(store, conversation_id=conversation_id)),
    ]
