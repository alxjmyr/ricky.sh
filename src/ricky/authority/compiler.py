"""Deterministic authority compilation for immutable execution contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ricky.authority.registry import AuthorityRegistry, default_authority_registry
from ricky.authority.store import AuthorityStore
from ricky.authority.types import (
    AuthorityScope,
    DelegationGrant,
    GrantSource,
    source_text_digest,
)
from ricky.config import RickySettings
from ricky.executions.contracts import ExecutionContract
from ricky.messaging.types import InboundMessage


class AuthorityCompilerError(RuntimeError):
    """Delegation could not be compiled safely."""


def build_grant_source(
    inbound: InboundMessage,
    *,
    conversation_id: str,
    snapshot_chars: int,
) -> GrantSource:
    """Derive the sole legitimate source of authority from an accepted message.

    Only an accepted inbound user message may reach this function; a rejected
    message, a model message, a tool result, or a notification never can.
    """

    if inbound.status == "rejected":
        raise AuthorityCompilerError("a rejected message cannot source authority")
    return GrantSource(
        principal_id=principal_id(inbound),
        transport=inbound.transport,
        account=inbound.account,
        sender_id=inbound.sender_id,
        destination_id=inbound.destination_id,
        platform_message_id=inbound.platform_message_id,
        inbound_message_id=inbound.id,
        conversation_id=conversation_id,
        text_digest=source_text_digest(inbound.text),
        text_snapshot=inbound.text[:snapshot_chars],
        received_at=inbound.received_at,
    )


def principal_id(inbound: InboundMessage) -> str:
    """The stable authenticated principal identity for one transport sender."""

    return f"{inbound.transport}:{inbound.account}:{inbound.sender_id}"


class ContractAuthorityCompiler:
    """Issue effect authority bound to one immutable execution contract."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        registry: AuthorityRegistry | None = None,
        store: AuthorityStore | None = None,
    ) -> None:
        self.settings = settings
        self.policy = settings.authority
        self.registry = registry or default_authority_registry()
        self.store = store or AuthorityStore(settings)

    async def compile(
        self,
        contract: ExecutionContract,
        *,
        source: GrantSource,
        now: datetime | None = None,
    ) -> DelegationGrant | None:
        authority_caps = [
            item for item in contract.capabilities if item.authority_capability is not None
        ]
        if not authority_caps:
            return None
        moment = now or datetime.now(UTC)
        if not self.policy.enabled:
            raise AuthorityCompilerError("delegated authority is disabled by configuration")
        if source.principal_id != contract.principal_id or (
            source.principal_id not in self.policy.allowed_principals
        ):
            raise AuthorityCompilerError("source principal cannot authorize this contract")
        if source.conversation_id != contract.source_conversation_id or (
            source.inbound_message_id not in contract.source_message_ids
        ):
            raise AuthorityCompilerError("authority source is outside the execution contract")

        guardrails = {item.capability_id: item for item in contract.guardrails}
        scopes: list[AuthorityScope] = []
        summaries: list[str] = []
        ceilings = []
        money_ceilings = []
        money_requests: list[tuple[int, str | None]] = []
        contract_tools = {item.id for item in contract.tools}
        for capability in authority_caps:
            assert capability.authority_capability is not None
            semantic = capability.authority_capability
            ceiling = self.policy.capabilities.get(semantic)
            if ceiling is None or not ceiling.enabled:
                raise AuthorityCompilerError(
                    f"effect capability is disabled by owner policy: {semantic}"
                )
            if not set(contract.profile_scope.profiles).issubset(ceiling.allowed_profiles):
                raise AuthorityCompilerError(
                    "effect capability is unavailable for profile scope "
                    f"{contract.profile_scope.profiles}: {semantic}"
                )
            evaluator = self.registry.require(semantic)
            if not evaluator.tools <= contract_tools:
                missing = ", ".join(sorted(evaluator.tools - contract_tools))
                raise AuthorityCompilerError(
                    f"execution contract is missing evaluator tools: {missing}"
                )
            guardrail = guardrails.get(capability.id)
            if guardrail is None:
                raise AuthorityCompilerError(
                    f"effect capability requires a compiled guardrail: {capability.id}"
                )
            if (
                guardrail.schema_id != evaluator.schema_id
                or guardrail.schema_version != evaluator.schema_version
            ):
                raise AuthorityCompilerError(
                    f"effect evaluator schema differs from contract: {capability.id}"
                )
            scope = AuthorityScope(
                capability=semantic,
                schema_id=guardrail.schema_id,
                schema_version=guardrail.schema_version,
                constraints=guardrail.constraints,
            )
            scopes.append(scope)
            summaries.append(guardrail.summary)
            ceilings.append(ceiling)
            constraints = guardrail.constraints
            if isinstance(constraints, dict):
                raw_amount = 0
                for key in (
                    "financial_limit_minor",
                    "max_amount_minor",
                    "deposit_limit_minor",
                ):
                    candidate = constraints.get(key)
                    if isinstance(candidate, int):
                        raw_amount = candidate
                        break
                raw_currency = constraints.get("currency")
                money_requests.append(
                    (raw_amount, raw_currency if isinstance(raw_currency, str) else None)
                )
                if raw_amount > 0:
                    money_ceilings.append(ceiling)
            if getattr(evaluator, "uses_owner_financial_ceiling", False):
                money_requests.append((ceiling.max_financial_limit_minor, ceiling.currency))
                if ceiling.max_financial_limit_minor > 0:
                    money_ceilings.append(ceiling)

        effect_limit = min(
            contract.budget.effect_calls,
            self.policy.max_effect_calls,
            *(item.max_effect_calls for item in ceilings),
        )
        if effect_limit < 1:
            raise AuthorityCompilerError("contract and owner policy allow no effect calls")
        ttl_limit = min(
            self.policy.max_ttl_seconds,
            *(item.max_ttl_seconds for item in ceilings),
        )
        expiry = moment + timedelta(seconds=ttl_limit)
        if contract.expires_at is not None:
            expiry = min(expiry, contract.expires_at)
        if expiry <= moment:
            raise AuthorityCompilerError("execution contract expired before grant issue")

        requested_money = max((amount for amount, _ in money_requests), default=0)
        currencies = {currency for amount, currency in money_requests if amount > 0}
        if len(currencies) > 1:
            raise AuthorityCompilerError("one grant cannot mix currencies")
        currency = next(iter(currencies), None)
        financial_limit: int | None = None
        if requested_money > 0:
            policy_currencies = {
                item.currency for item in money_ceilings if item.currency is not None
            }
            if currency is None or policy_currencies != {currency}:
                raise AuthorityCompilerError("guardrail currency differs from owner policy")
            financial_limit = min(
                requested_money,
                *(item.max_financial_limit_minor for item in money_ceilings),
            )
            if financial_limit < requested_money:
                raise AuthorityCompilerError(
                    "compiled guardrail exceeds the current financial owner ceiling"
                )

        grant = DelegationGrant(
            id=f"grant_{uuid4().hex}",
            source=source,
            task_id=contract.task_id,
            task_revision=contract.task_revision,
            profile_scope=contract.profile_scope,
            execution_request_id=None,
            contract_id=contract.id,
            contract_digest=contract.digest,
            confirmations=contract.confirmations,
            scopes=tuple(scopes),
            summary=" ".join(summaries)[:4_000],
            effect_call_limit=effect_limit,
            financial_limit_minor=financial_limit,
            currency=currency if financial_limit is not None else None,
            issued_at=moment,
            expires_at=expiry,
            status="active",
            policy_digest=self.policy.digest(),
        )
        await self.store.initialize()
        await self.store.issue(grant, scope=contract.profile_scope)
        return grant
