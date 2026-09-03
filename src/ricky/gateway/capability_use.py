"""Source-bound live review for direct foreground capability calls."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ricky.capabilities import (
    AuthenticatedSource,
    CapabilityPolicyDecision,
    CapabilityRegistry,
    CollectedGuardrailField,
    CompiledGuardrail,
    GuardrailProposal,
    GuardrailRegistry,
)
from ricky.capabilities.policy import policy_digest
from ricky.config import GatewayRouteSettings, RickySettings
from ricky.executions.contracts import ConfirmationRef
from ricky.executions.drafts import (
    DraftActivityKind,
    ExecutionDraft,
    ForegroundCapabilityCall,
    summary_digest,
)
from ricky.executions.store import ExecutionDraftFenceError, ExecutionStore
from ricky.gateway.types import Conversation
from ricky.messaging.types import InboundMessage
from ricky.tools import Tool, ToolContext, ToolRegistry, ToolResult, UserInteractionRequest


class ForegroundCapabilityUseError(RuntimeError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CapabilityUseProposal(_StrictModel):
    tool_name: str = Field(min_length=1, max_length=300)
    arguments: dict[str, JsonValue]
    guardrail: GuardrailProposal | None = None
    draft_id: str | None = Field(default=None, pattern=r"^draft_[0-9a-f]{32}$")
    expected_draft_revision: int | None = Field(default=None, ge=1)
    confirm: bool = False


class PrepareCapabilityUseParams(_StrictModel):
    proposal: CapabilityUseProposal


class CapabilityUseReviewResult(_StrictModel):
    status: str
    draft_id: str
    draft_revision: int
    questions: list[str] = Field(default_factory=list)
    confirmation_summary: str | None = None


class ForegroundCapabilityUseManager:
    """Compile one exact direct call without creating a session-wide grant."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        registry: CapabilityRegistry,
        guardrails: GuardrailRegistry,
        decisions: dict[str, CapabilityPolicyDecision],
        tools: dict[str, Tool],
        route: GatewayRouteSettings,
        conversation: Conversation,
        inbound: InboundMessage,
        store: ExecutionStore,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.guardrails = guardrails
        self.decisions = decisions
        self.tools = tools
        self.tool_registry = ToolRegistry(tuple(tools.values()))
        self.route = route
        self.conversation = conversation
        self.inbound = inbound
        self.store = store

    def source(self) -> AuthenticatedSource:
        text = self.inbound.text[: self.settings.executions.source_snapshot_chars]
        return AuthenticatedSource(
            principal_id=(
                f"{self.inbound.transport}:{self.inbound.account}:{self.inbound.sender_id}"
            ),
            conversation_id=self.conversation.id,
            message_id=self.inbound.id,
            text_digest=hashlib.sha256(self.inbound.text.encode("utf-8")).hexdigest(),
            text_snapshot=text,
            received_at=self.inbound.received_at,
        )

    async def review(self, proposal: CapabilityUseProposal) -> ExecutionDraft:
        source = self.source()
        moment = datetime.now(UTC)
        tool, decision, call, arguments = self._resolve_call(proposal)
        del tool
        if not decision.confirmation_required and not decision.guardrail_required:
            raise ForegroundCapabilityUseError(
                "this capability needs no live preparation; call the tool directly"
            )
        await self.store.initialize()
        current = None
        if proposal.draft_id is not None:
            if proposal.expected_draft_revision is None:
                raise ForegroundCapabilityUseError("draft continuation requires a revision")
            current = await self.store.get_draft(
                proposal.draft_id, scope=self.conversation.profile_scope
            )
            if current.revision != proposal.expected_draft_revision:
                raise ForegroundCapabilityUseError(
                    f"stale draft revision: expected {proposal.expected_draft_revision}, "
                    f"found {current.revision}"
                )
            self._validate_continuation(current, call, source, moment)
        elif proposal.expected_draft_revision is not None or proposal.confirm:
            raise ForegroundCapabilityUseError(
                "confirmation and expected revision require an existing draft"
            )

        if current is not None and proposal.confirm:
            return await self._confirm(current, source, moment)

        sources = (
            tuple((*current.sources, source))
            if current is not None
            and source.message_id not in {item.message_id for item in current.sources}
            else (current.sources if current is not None else (source,))
        )
        compiled = current.guardrails if current is not None else ()
        collected = {
            item.field: item
            for item in (current.collected_guardrail_fields if current is not None else ())
        }
        questions: list[str] = []
        if decision.guardrail_required:
            evaluator = self.guardrails.get(decision.capability_id)
            if evaluator is None:
                raise ForegroundCapabilityUseError("required guardrail evaluator is unavailable")
            candidate = proposal.guardrail
            if candidate is not None:
                for field_proposal in candidate.fields:
                    if evaluator.intake_spec.get(field_proposal.field) is None:
                        raise ForegroundCapabilityUseError(
                            f"unknown guardrail field: {field_proposal.field}"
                        )
                    normalized = evaluator.normalize_field(field_proposal)
                    if normalized.reason is not None:
                        raise ForegroundCapabilityUseError(normalized.reason)
                    if normalized.question is not None:
                        questions.append(normalized.question)
                        compiled = ()
                        continue
                    collected[field_proposal.field] = CollectedGuardrailField(
                        capability_id=decision.capability_id,
                        schema_id=evaluator.schema_id,
                        schema_version=evaluator.schema_version,
                        field=field_proposal.field,
                        value=normalized.value,
                        source_message_id=source.message_id,
                        source_text_digest=source.text_digest,
                        source_quote=field_proposal.source_quote,
                    )
                    compiled = ()
            if not compiled:
                outcome = evaluator.validate_collected(
                    tuple(collected[key] for key in sorted(collected)), sources
                )
                if outcome.reason is not None:
                    raise ForegroundCapabilityUseError(outcome.reason)
                if outcome.questions:
                    questions.extend(outcome.questions)
                    compiled = ()
                else:
                    assert outcome.guardrail is not None
                    compiled = (outcome.guardrail,)

        summary = _confirmation_summary(call, compiled)
        if questions:
            status = "collecting_guardrails"
            confirmation_summary = None
            confirmation_digest = None
        elif decision.confirmation_required:
            status = "awaiting_confirmation"
            confirmation_summary = summary
            confirmation_digest = summary_digest(summary)
        else:
            status = "ready"
            confirmation_summary = summary
            confirmation_digest = summary_digest(summary)
        revision = 1 if current is None else current.revision + 1
        created_at = current.created_at if current is not None else moment
        task_id, task_revision = _task_link(arguments)
        draft = ExecutionDraft(
            id=(current.id if current is not None else _foreground_draft_id(source, call)),
            target="gateway_foreground",
            status=status,
            revision=revision,
            principal_id=source.principal_id,
            conversation_id=source.conversation_id,
            task_id=task_id,
            task_revision=task_revision,
            profile_scope=self.conversation.profile_scope,
            goal=call.safe_summary,
            requested_capabilities=(decision.capability_id,),
            sources=sources,
            guardrails=compiled,
            collected_guardrail_fields=tuple(collected[key] for key in sorted(collected)),
            pending_questions=tuple(dict.fromkeys(questions)),
            confirmation_required=decision.confirmation_required,
            confirmation_summary=confirmation_summary,
            confirmation_summary_digest=confirmation_digest,
            agent_policy_digest=self.settings.agents.gateway_foreground.digest(),
            route_policy_digest=policy_digest(self.settings.agents.gateway_foreground, self.route),
            inventory_digest=self.registry.digest(),
            created_at=created_at,
            updated_at=moment,
            expires_at=created_at + timedelta(seconds=self.settings.executions.draft_ttl_seconds),
            foreground_call=call,
        )
        if current is None:
            return await self.store.create_draft(draft, scope=self.conversation.profile_scope)
        kind: DraftActivityKind = "guardrails_updated" if questions else "ready"
        if status == "awaiting_confirmation":
            kind = "confirmation_requested"
        return await self.store.update_draft(
            draft,
            expected_revision=current.revision,
            kind=kind,
            summary="Direct capability-use review updated",
            scope=self.conversation.profile_scope,
        )

    async def reserve(self, tool_name: str, arguments: dict[str, object]) -> ExecutionDraft | None:
        definition = self.registry.for_resource("tool", tool_name)
        if definition is None:
            raise ForegroundCapabilityUseError(f"tool has no capability owner: {tool_name}")
        decision = self.decisions[definition.id]
        if not decision.confirmation_required and not decision.guardrail_required:
            return None
        digest = _arguments_digest(arguments)
        candidates = await self.store.list_drafts(
            scope=self.conversation.profile_scope, status="ready", limit=1_000
        )
        draft = next(
            (
                item
                for item in candidates
                if item.target == "gateway_foreground"
                and item.conversation_id == self.conversation.id
                and item.principal_id == self.source().principal_id
                and item.foreground_call is not None
                and item.foreground_call.tool_name == tool_name
                and item.foreground_call.arguments_digest == digest
            ),
            None,
        )
        if draft is None:
            raise ForegroundCapabilityUseError(
                "this exact call requires a live capability-use draft; call "
                "prepare_capability_use first"
            )
        now = datetime.now(UTC)
        if draft.expires_at <= now:
            raise ForegroundCapabilityUseError("the capability-use draft expired")
        if decision.confirmation_required and (
            draft.confirmation is None or draft.confirmation.expires_at <= now
        ):
            raise ForegroundCapabilityUseError("the capability-use confirmation expired")
        if draft.agent_policy_digest != self.settings.agents.gateway_foreground.digest() or (
            draft.route_policy_digest
            != policy_digest(self.settings.agents.gateway_foreground, self.route)
        ):
            raise ForegroundCapabilityUseError("foreground capability policy changed")
        if draft.inventory_digest != self.registry.digest():
            raise ForegroundCapabilityUseError("foreground capability inventory changed")
        if decision.guardrail_required:
            evaluator = self.guardrails.get(definition.id)
            if evaluator is None or not draft.guardrails:
                raise ForegroundCapabilityUseError("compiled direct-call guardrail is missing")
            verdict = evaluator.evaluate_call(
                draft.guardrails[0], tool_name, arguments, usage=_zero_usage()
            )
            if not verdict.allowed:
                raise ForegroundCapabilityUseError(verdict.reason)
        executing = draft.model_copy(
            update={"status": "executing", "revision": draft.revision + 1, "updated_at": now}
        )
        return await self.store.update_draft(
            executing,
            expected_revision=draft.revision,
            kind="executing",
            summary="Exact foreground call reserved",
            scope=self.conversation.profile_scope,
        )

    async def finish(self, draft: ExecutionDraft, *, uncertain: bool, reason: str) -> None:
        current = await self.store.get_draft(draft.id, scope=self.conversation.profile_scope)
        if current.status != "executing" or current.revision != draft.revision:
            raise ExecutionDraftFenceError("foreground capability-use reservation changed")
        finished = current.model_copy(
            update={
                "status": "uncertain" if uncertain else "completed",
                "revision": current.revision + 1,
                "updated_at": datetime.now(UTC),
                "reason": reason[:2_000],
            }
        )
        await self.store.update_draft(
            finished,
            expected_revision=current.revision,
            kind="uncertain" if uncertain else "completed",
            summary=reason,
            scope=self.conversation.profile_scope,
        )

    def _resolve_call(self, proposal: CapabilityUseProposal):
        tool = self.tools.get(proposal.tool_name)
        if tool is None:
            raise ForegroundCapabilityUseError(
                f"foreground tool is unavailable: {proposal.tool_name}"
            )
        definition = self.registry.for_resource("tool", tool.name)
        if definition is None:
            raise ForegroundCapabilityUseError(f"tool has no capability owner: {tool.name}")
        decision = self.decisions.get(definition.id)
        if decision is None or not decision.eligible:
            raise ForegroundCapabilityUseError(f"capability is excluded: {definition.id}")
        prepared = self.tool_registry.prepare_args(tool.name, proposal.arguments)
        if prepared.error is not None:
            raise ForegroundCapabilityUseError(prepared.error.content)
        assert prepared.args is not None
        arguments = tool.Params.model_validate(prepared.args).model_dump(mode="json")
        call = ForegroundCapabilityCall(
            capability_id=definition.id,
            tool_name=tool.name,
            arguments_digest=_arguments_digest(arguments),
            safe_summary=_safe_call_summary(definition.id, tool.name, arguments),
        )
        if proposal.guardrail is not None and (proposal.guardrail.capability_id != definition.id):
            raise ForegroundCapabilityUseError("guardrail belongs to another capability")
        return tool, decision, call, arguments

    def _validate_continuation(
        self,
        current: ExecutionDraft,
        call: ForegroundCapabilityCall,
        source: AuthenticatedSource,
        now: datetime,
    ) -> None:
        if current.status not in {"collecting_guardrails", "awaiting_confirmation"}:
            raise ForegroundCapabilityUseError(
                f"capability-use draft is {current.status}; it cannot be continued"
            )
        if current.target != "gateway_foreground" or current.foreground_call != call:
            raise ForegroundCapabilityUseError("draft continuation changes the exact proposed call")
        if current.principal_id != source.principal_id or (
            current.conversation_id != source.conversation_id
        ):
            raise ForegroundCapabilityUseError("draft belongs to another authenticated source")
        if current.expires_at <= now:
            raise ForegroundCapabilityUseError("capability-use draft expired")
        if current.agent_policy_digest != self.settings.agents.gateway_foreground.digest():
            raise ForegroundCapabilityUseError("foreground capability policy changed")
        if current.route_policy_digest != policy_digest(
            self.settings.agents.gateway_foreground, self.route
        ):
            raise ForegroundCapabilityUseError("foreground route policy changed")
        if current.inventory_digest != self.registry.digest():
            raise ForegroundCapabilityUseError("foreground capability inventory changed")

    async def _confirm(
        self, current: ExecutionDraft, source: AuthenticatedSource, now: datetime
    ) -> ExecutionDraft:
        if current.status != "awaiting_confirmation" or not _is_affirmative(source.text_snapshot):
            raise ForegroundCapabilityUseError("exact confirmation requires a standalone yes")
        assert current.confirmation_summary_digest is not None
        confirmation = ConfirmationRef(
            id=f"confirmation_{uuid4().hex}",
            draft_id=current.id,
            draft_revision=current.revision,
            principal_id=current.principal_id,
            source_message_id=source.message_id,
            summary_digest=current.confirmation_summary_digest,
            confirmed_at=now,
            expires_at=min(
                current.expires_at,
                now + timedelta(seconds=self.settings.executions.confirmation_ttl_seconds),
            ),
        )
        sources = (
            current.sources
            if source.message_id in {item.message_id for item in current.sources}
            else (*current.sources, source)
        )
        ready = current.model_copy(
            update={
                "status": "ready",
                "revision": current.revision + 1,
                "sources": sources,
                "confirmation": confirmation,
                "updated_at": now,
            }
        )
        return await self.store.update_draft(
            ready,
            expected_revision=current.revision,
            kind="confirmed",
            summary="Exact foreground call confirmed",
            scope=self.conversation.profile_scope,
        )


class PrepareCapabilityUseTool:
    name: ClassVar[str] = "prepare_capability_use"
    description: ClassVar[str] = (
        "Prepare an exact direct foreground tool call when its capability requires live "
        "guardrails or confirmation. This never grants a session-wide permission."
    )
    Params: ClassVar[type[BaseModel]] = PrepareCapabilityUseParams
    Result: ClassVar[type[BaseModel]] = CapabilityUseReviewResult
    risk: ClassVar[str] = "mutating"
    capability_id = "builtin.authorization.review"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, manager: ForegroundCapabilityUseManager) -> None:
        self.manager = manager

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = PrepareCapabilityUseParams.model_validate(params)
        try:
            draft = await self.manager.review(args.proposal)
        except ForegroundCapabilityUseError as exc:
            return ToolResult(content=str(exc), is_error=True)
        result = CapabilityUseReviewResult(
            status=draft.status,
            draft_id=draft.id,
            draft_revision=draft.revision,
            questions=list(draft.pending_questions),
            confirmation_summary=draft.confirmation_summary,
        )
        if draft.status == "collecting_guardrails":
            prompt = "\n".join(f"- {item}" for item in draft.pending_questions)
            content = (
                f"Draft {draft.id} at revision {draft.revision}. The turn will stop after "
                "delivering the exact guardrail questions."
            )
        elif draft.status == "awaiting_confirmation":
            prompt = (
                cast(str, draft.confirmation_summary)
                + "\n\nReply Yes to approve this exact capability use."
            )
            content = (
                f"Draft {draft.id} at revision {draft.revision}. The turn will stop after "
                "delivering the exact confirmation summary."
            )
        else:
            prompt = None
            content = (
                f"Exact foreground call is authorized by draft {draft.id}; "
                "call the prepared tool with the identical arguments once."
            )
        interaction = None
        if prompt is not None:
            interaction = UserInteractionRequest(
                kind=(
                    "guardrail_input" if draft.status == "collecting_guardrails" else "confirmation"
                ),
                correlation_id=f"{draft.id}:{draft.revision}",
                prompt=prompt,
            )
        return ToolResult(
            content=content,
            data=result.model_dump(mode="json"),
            user_interaction=interaction,
        )


class ForegroundAuthorizedTool:
    """One-time exact-call gate around a normal foreground tool."""

    def __init__(self, tool: Tool, manager: ForegroundCapabilityUseManager) -> None:
        self._tool = tool
        self._manager = manager
        self.name = tool.name
        self.description = tool.description
        self.Params = tool.Params
        self.risk = tool.risk
        declared = cast(Any, tool)
        self.capability_id = declared.capability_id
        self.effect_kind = declared.effect_kind
        self.unattended = declared.unattended
        self.state_guard_id = declared.state_guard_id
        self.review_mode = getattr(declared, "review_mode", "policy")
        for name in (
            "Result",
            "workflow_only",
            "deferred_until_artifact",
            "result_is_bounded",
        ):
            if hasattr(tool, name):
                setattr(self, name, getattr(tool, name))

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        arguments = params.model_dump(mode="json")
        try:
            draft = await self._manager.reserve(self.name, arguments)
        except ForegroundCapabilityUseError as exc:
            return ToolResult(content=str(exc), is_error=True)
        try:
            result = await self._tool.run(params, ctx)
        except BaseException as exc:
            if draft is not None:
                await self._manager.finish(
                    draft,
                    uncertain=True,
                    reason=f"direct call interrupted: {type(exc).__name__}",
                )
            raise
        if draft is not None:
            receipt = result.effect_receipt
            uncertain = self.effect_kind == "external" and (
                receipt is None or receipt.disposition == "in_doubt"
            )
            await self._manager.finish(
                draft,
                uncertain=uncertain,
                reason=(
                    "Exact foreground call has no confirmed external-effect receipt"
                    if uncertain
                    else "Exact foreground call completed"
                ),
            )
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool, name)


def _arguments_digest(arguments: dict[str, object]) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _foreground_draft_id(
    source: AuthenticatedSource,
    call: ForegroundCapabilityCall,
) -> str:
    identity = (
        f"gateway_foreground:{source.conversation_id}:{source.message_id}:"
        f"{call.capability_id}:{call.tool_name}:{call.arguments_digest}"
    )
    return "draft_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _safe_call_summary(
    capability_id: str,
    tool_name: str,
    arguments: dict[str, object],
) -> str:
    redacted = _redact(arguments)
    return (
        f"Use {capability_id} via {tool_name} once with "
        f"{json.dumps(redacted, sort_keys=True, ensure_ascii=False)}"
    )[:4_000]


def _redact(value: object, key: str = "") -> object:
    sensitive = {"secret", "token", "password", "credential", "authorization", "api_key"}
    if any(part in key.lower() for part in sensitive):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(name): _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    text = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
    if isinstance(text, str) and len(text) > 500:
        return text[:500] + "…"
    return text


def _confirmation_summary(
    call: ForegroundCapabilityCall,
    guardrails: tuple[CompiledGuardrail, ...],
) -> str:
    lines = [call.safe_summary]
    if guardrails:
        lines.append("Guardrails:")
        lines.extend(f"- {item.summary}" for item in guardrails)
    lines.append(f"Exact argument digest: {call.arguments_digest}")
    return "\n".join(lines)[:8_000]


def _task_link(arguments: dict[str, object]) -> tuple[str | None, int | None]:
    task_id = arguments.get("task_id")
    revision = arguments.get("task_revision") or arguments.get("expected_revision")
    if isinstance(task_id, str) and isinstance(revision, int) and revision >= 1:
        return task_id, revision
    return None, None


def _is_affirmative(text: str) -> bool:
    normalized = " ".join(text.strip().lower().rstrip(".! ").split())
    return normalized in {"yes", "confirm", "confirmed", "proceed", "approve", "approved"}


def _zero_usage():
    from ricky.capabilities import GuardrailUsage

    return GuardrailUsage()
