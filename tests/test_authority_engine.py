"""Profile, grant, policy, and effect guards all constrain a call."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel

from authority_support import (
    COMPLETE_CONSTRAINTS,
    VALID_CALL,
    ambiguous_dispatch,
    authority_registry,
    grant_source,
    settings,
)
from ricky.agent.session import AgentSession
from ricky.authority.engine import (
    DelegatedAuthorityError,
    DelegatedEffectTool,
    DelegatedRun,
    build_delegated_tools,
)
from ricky.authority.registry import AuthorityRegistry
from ricky.authority.store import AuthorityStore
from ricky.authority.types import AuthorityScope, DelegationGrant
from ricky.browser.authority import BrowserInteractAuthorityEvaluator
from ricky.config import RickySettings
from ricky.jobs.store import GrantAuthorityError, JobActionConflictError, JobRunStore
from ricky.jobs.types import JobRun
from ricky.tools import EffectIdentity, Tool, ToolContext
from sandbox_support import (
    SandboxOutcome,
    SandboxReservationTool,
    SandboxReserveParams,
)

TASK_ID = "task_" + "b" * 32


def _grant(config: RickySettings, **overrides: Any) -> DelegationGrant:
    issued = datetime.now(UTC)
    payload: dict[str, Any] = {
        "id": f"grant_{uuid4().hex}",
        "source": grant_source(),
        "task_id": TASK_ID,
        "task_revision": 1,
        "profile_scope": config.resolve_profile_scope("personal"),
        "contract_id": "contract_" + "c" * 32,
        "contract_digest": "d" * 64,
        "scopes": (
            AuthorityScope(
                capability="sandbox_reservation",
                schema_id="sandbox.reservation",
                schema_version=1,
                constraints=dict(COMPLETE_CONSTRAINTS),
            ),
        ),
        "summary": "Make at most one sandbox reservation.",
        "effect_call_limit": 1,
        "issued_at": issued,
        "expires_at": issued + timedelta(hours=1),
        "status": "active",
        "policy_digest": config.authority.digest(),
    }
    payload.update(overrides)
    return DelegationGrant.model_validate(payload)


class _Harness:
    def __init__(self, config: RickySettings) -> None:
        self.settings = config
        self.jobs = JobRunStore(config)
        self.authority = AuthorityStore(config)
        self.scope = config.resolve_profile_scope("personal")
        self.run_id = f"jobrun_{uuid4().hex}"

    async def start(
        self,
        *,
        grant: DelegationGrant,
        dispatch: Any = None,
        effect_budget: int = 1,
        registry: AuthorityRegistry | None = None,
    ) -> DelegatedEffectTool:
        await self.jobs.initialize()
        await self.authority.initialize()
        await self.jobs.insert(
            JobRun(
                id=self.run_id,
                provider="scripted",
                model="test-model",
                profile_scope=grant.profile_scope,
                session_id=f"session_{uuid4().hex}",
                started_at=datetime.now(UTC),
            ),
            scope=grant.profile_scope,
        )
        await self.authority.issue(grant, scope=grant.profile_scope)
        await self.jobs.seed_grant_budget(
            grant_id=grant.id,
            task_id=grant.task_id,
            effect_limit=grant.effect_call_limit,
            financial_limit_minor=grant.financial_limit_minor,
            currency=grant.currency,
            expires_at=grant.expires_at,
            scope=grant.profile_scope,
        )
        return DelegatedEffectTool(
            cast(Tool, SandboxReservationTool(self.settings, dispatch=dispatch)),
            grant=grant,
            registry=registry or authority_registry(),
            authority=self.authority,
            jobs=self.jobs,
            run_id=self.run_id,
            effect_budget=effect_budget,
        )

    def ctx(self) -> ToolContext:
        return ToolContext(
            cwd=Path.cwd(),
            settings=self.settings,
            session=AgentSession.create(
                self.settings,
                profile_scope=self.scope,
                provider="openrouter",
                model="test-model",
            ),
        )


def _params(**overrides: Any) -> BaseModel:
    payload = dict(VALID_CALL)
    payload.update(overrides)
    return SandboxReserveParams.model_validate(payload)


def test_persistent_resource_open_is_scoped_but_not_a_delegated_effect(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    grant = _grant(
        config,
        scopes=(
            AuthorityScope(
                capability="browser_interact",
                schema_id="browser.interact",
                schema_version=1,
                constraints={
                    "version": 1,
                    "capability_id": "builtin.browser.interact",
                    "mode": "read_only",
                    "allowed_tools": ["browser_session_open_resource"],
                    "resources": [
                        {"profile": "personal", "name": "research"},
                    ],
                    "authenticated_origins": [
                        {
                            "resource": {"profile": "personal", "name": "research"},
                            "origins": ["https://accounts.example.com"],
                        }
                    ],
                    "allow_ephemeral": False,
                    "allow_public_https_research": False,
                    "allow_masked_visual_observations": False,
                    "private_origin_ceiling": [],
                    "attachment_ids": [],
                    "protected_values": [],
                },
            ),
        ),
    )
    delegated = DelegatedRun(
        grant=grant,
        registry=AuthorityRegistry([BrowserInteractAuthorityEvaluator()]),
        authority=AuthorityStore(config),
    )

    assert delegated.tool_names() == frozenset()


async def test_a_call_inside_the_grant_is_performed_and_consumes_the_grant(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)
    grant = _grant(config)
    tool = await harness.start(grant=grant)

    result = await tool.run(_params(), harness.ctx())

    assert not result.is_error
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "performed"
    assert f"grant_id: {grant.id}" in result.content
    assert (await harness.authority.get(grant.id, scope=harness.scope)).status == "consumed"
    kinds = [
        item.kind for item in await harness.authority.activities(grant.id, scope=harness.scope)
    ]
    assert kinds == ["issued", "used", "consumed"]


async def test_grant_budget_termination_survives_seed_order_and_repetition(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    jobs = JobRunStore(config)
    await jobs.initialize()
    expires_at = datetime.now(UTC) + timedelta(hours=1)

    revoked_before_seed = f"grant_{uuid4().hex}"
    scope = config.resolve_profile_scope("personal")
    await jobs.set_grant_budget_status(revoked_before_seed, "revoked", scope=scope)
    await jobs.seed_grant_budget(
        grant_id=revoked_before_seed,
        task_id=TASK_ID,
        effect_limit=1,
        financial_limit_minor=None,
        currency=None,
        expires_at=expires_at,
        scope=scope,
    )
    await jobs.seed_grant_budget(
        grant_id=revoked_before_seed,
        task_id=TASK_ID,
        effect_limit=1,
        financial_limit_minor=None,
        currency=None,
        expires_at=expires_at,
        scope=scope,
    )
    first = await jobs.get_grant_budget(revoked_before_seed, scope=scope)
    assert first is not None and first["status"] == "revoked"

    revoked_after_seed = f"grant_{uuid4().hex}"
    await jobs.seed_grant_budget(
        grant_id=revoked_after_seed,
        task_id=TASK_ID,
        effect_limit=1,
        financial_limit_minor=None,
        currency=None,
        expires_at=expires_at,
        scope=scope,
    )
    await jobs.set_grant_budget_status(revoked_after_seed, "revoked", scope=scope)
    await jobs.seed_grant_budget(
        grant_id=revoked_after_seed,
        task_id=TASK_ID,
        effect_limit=1,
        financial_limit_minor=None,
        currency=None,
        expires_at=expires_at,
        scope=scope,
    )
    second = await jobs.get_grant_budget(revoked_after_seed, scope=scope)
    assert second is not None and second["status"] == "revoked"

    await jobs.insert(
        JobRun(
            id="jobrun_missing",
            provider="scripted",
            model="test-model",
            profile_scope=scope,
            session_id=f"session_{uuid4().hex}",
            started_at=datetime.now(UTC),
        ),
        scope=scope,
    )
    with pytest.raises(GrantAuthorityError, match="revoked"):
        await jobs.reserve_grant_action(
            grant_id=revoked_before_seed,
            namespace="grant:test",
            task_id=TASK_ID,
            run_id="jobrun_missing",
            identity=EffectIdentity(
                action_key="a" * 64,
                operation="test",
                target="target",
                occurrence="once",
                summary="test",
            ),
            effect_budget=1,
            scope=scope,
        )


async def test_a_call_outside_the_grant_is_denied_and_recorded(tmp_path: Path) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)
    grant = _grant(config)
    tool = await harness.start(grant=grant)

    outside_window = await tool.run(_params(arrival_time="21:30"), harness.ctx())
    assert outside_window.is_error
    assert "outside the granted window" in outside_window.content
    assert outside_window.effect_receipt is not None
    assert outside_window.effect_receipt.disposition == "not_performed"

    other_venue = await tool.run(_params(venue_id="venue-b"), harness.ctx())
    assert other_venue.is_error

    bigger_party = await tool.run(_params(party_size=8), harness.ctx())
    assert bigger_party.is_error

    other_account = await tool.run(_params(account_identity="someone@else"), harness.ctx())
    assert other_account.is_error

    # A denial never consumes an effect call and is always inspectable.
    assert (await harness.authority.get(grant.id, scope=harness.scope)).status == "active"
    denials = [
        item
        for item in await harness.authority.activities(grant.id, scope=harness.scope)
        if item.kind == "denied"
    ]
    assert len(denials) == 4
    budget = await harness.jobs.get_grant_budget(grant.id, scope=harness.scope)
    assert budget is not None and budget["effects_used"] == 0


async def test_a_deposit_beyond_the_grant_is_denied(tmp_path: Path) -> None:
    config = settings(tmp_path, max_financial_limit_minor=2_500, currency="USD")
    harness = _Harness(config)
    priced = dict(COMPLETE_CONSTRAINTS)
    priced["deposit_limit_minor"] = 1_000
    priced["currency"] = "USD"
    grant = _grant(
        config,
        scopes=(
            AuthorityScope(
                capability="sandbox_reservation",
                schema_id="sandbox.reservation",
                schema_version=1,
                constraints=priced,
            ),
        ),
        financial_limit_minor=1_000,
        currency="USD",
    )
    tool = await harness.start(grant=grant)

    too_much = await tool.run(_params(deposit_minor=5_000, currency="USD"), harness.ctx())
    assert too_much.is_error and "deposit exceeds" in too_much.content

    allowed = await tool.run(_params(deposit_minor=1_000, currency="USD"), harness.ctx())
    assert not allowed.is_error
    budget = await harness.jobs.get_grant_budget(grant.id, scope=harness.scope)
    assert budget is not None and budget["financial_used_minor"] == 1_000


async def test_a_performed_effect_cannot_be_reserved_twice_for_one_identity(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path, max_effect_calls=5, capability_effect_calls=5)
    harness = _Harness(config)
    grant = _grant(config, effect_call_limit=3)
    await harness.start(grant=grant, effect_budget=3)
    evaluator = authority_registry().require("sandbox_reservation")
    identity = evaluator.effect_identity(grant.scopes[0], "sandbox_reserve", dict(VALID_CALL))

    action = await harness.jobs.reserve_grant_action(
        grant_id=grant.id,
        namespace=grant.effect_namespace(),
        task_id=grant.task_id,
        run_id=harness.run_id,
        identity=identity,
        effect_budget=3,
        scope=harness.scope,
    )
    await harness.jobs.resolve_action(
        action.id,
        "performed",
        scope=harness.scope,
        provider_reference="ref-1",
    )

    with pytest.raises(JobActionConflictError):
        await harness.jobs.reserve_grant_action(
            grant_id=grant.id,
            namespace=grant.effect_namespace(),
            task_id=grant.task_id,
            run_id=harness.run_id,
            identity=identity,
            effect_budget=3,
            scope=harness.scope,
        )

    # A second grant for the same task shares the namespace, so it cannot replay it.
    later = _grant(config, effect_call_limit=1)
    await harness.authority.issue(later, scope=harness.scope)
    await harness.jobs.seed_grant_budget(
        grant_id=later.id,
        task_id=later.task_id,
        effect_limit=later.effect_call_limit,
        financial_limit_minor=None,
        currency=None,
        expires_at=later.expires_at,
        scope=harness.scope,
    )
    with pytest.raises(JobActionConflictError):
        await harness.jobs.reserve_grant_action(
            grant_id=later.id,
            namespace=later.effect_namespace(),
            task_id=later.task_id,
            run_id=harness.run_id,
            identity=identity,
            effect_budget=3,
            scope=harness.scope,
        )


async def test_grant_revocation_and_budget_seed_are_atomic_under_interleaving(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    jobs = JobRunStore(config)
    await jobs.initialize()
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    scope = config.resolve_profile_scope("personal")

    for _ in range(20):
        grant_id = f"grant_{uuid4().hex}"
        await asyncio.gather(
            jobs.seed_grant_budget(
                grant_id=grant_id,
                task_id=f"task_{uuid4().hex}",
                effect_limit=3,
                financial_limit_minor=None,
                currency=None,
                expires_at=expires_at,
                scope=scope,
            ),
            jobs.set_grant_budget_status(grant_id, "revoked", scope=scope),
        )

        budget = await jobs.get_grant_budget(grant_id, scope=scope)
        assert budget is not None
        assert budget["status"] == "revoked"


async def test_effect_and_financial_limits_are_atomic_under_concurrency(
    tmp_path: Path,
) -> None:
    config = settings(
        tmp_path,
        max_effect_calls=10,
        capability_effect_calls=10,
        max_financial_limit_minor=10_000,
        currency="USD",
    )
    harness = _Harness(config)
    grant = _grant(config, effect_call_limit=2, financial_limit_minor=1_500, currency="USD")
    await harness.start(grant=grant, effect_budget=10)
    evaluator = authority_registry().require("sandbox_reservation")

    async def reserve(index: int) -> str:
        scoped = dict(COMPLETE_CONSTRAINTS)
        scoped["local_date"] = f"2026-09-{index + 1:02d}"
        scope = AuthorityScope(
            capability="sandbox_reservation",
            schema_id="sandbox.reservation",
            schema_version=1,
            constraints=scoped,
        )
        identity = evaluator.effect_identity(scope, "sandbox_reserve", dict(VALID_CALL))
        try:
            await harness.jobs.reserve_grant_action(
                grant_id=grant.id,
                namespace=grant.effect_namespace(),
                task_id=grant.task_id,
                run_id=harness.run_id,
                identity=identity,
                effect_budget=10,
                amount_minor=1_000,
                currency="USD",
                scope=harness.scope,
            )
        except (GrantAuthorityError, JobActionConflictError) as exc:
            return str(exc)
        return "reserved"

    outcomes = await asyncio.gather(*(reserve(index) for index in range(8)))
    # Money runs out first: one call at 1000 minor units against a 1500 ceiling.
    assert outcomes.count("reserved") == 1
    assert any("financial limit exhausted" in item for item in outcomes)
    budget = await harness.jobs.get_grant_budget(grant.id, scope=harness.scope)
    assert budget is not None
    assert budget["effects_used"] == 1
    assert budget["financial_used_minor"] == 1_000


async def test_the_effect_call_limit_is_atomic_under_concurrency(tmp_path: Path) -> None:
    config = settings(tmp_path, max_effect_calls=10, capability_effect_calls=10)
    harness = _Harness(config)
    grant = _grant(config, effect_call_limit=2)
    await harness.start(grant=grant, effect_budget=10)
    evaluator = authority_registry().require("sandbox_reservation")

    async def reserve(index: int) -> bool:
        scoped = dict(COMPLETE_CONSTRAINTS)
        scoped["local_date"] = f"2026-09-{index + 1:02d}"
        scope = AuthorityScope(
            capability="sandbox_reservation",
            schema_id="sandbox.reservation",
            schema_version=1,
            constraints=scoped,
        )
        try:
            await harness.jobs.reserve_grant_action(
                grant_id=grant.id,
                namespace=grant.effect_namespace(),
                task_id=grant.task_id,
                run_id=harness.run_id,
                identity=evaluator.effect_identity(scope, "sandbox_reserve", dict(VALID_CALL)),
                effect_budget=10,
                scope=harness.scope,
            )
        except (GrantAuthorityError, JobActionConflictError):
            return False
        return True

    outcomes = await asyncio.gather(*(reserve(index) for index in range(8)))
    assert sum(outcomes) == 2
    budget = await harness.jobs.get_grant_budget(grant.id, scope=harness.scope)
    assert budget is not None and budget["effects_used"] == 2


async def test_a_performed_effect_consumes_a_single_reservation_capability(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path, max_effect_calls=5, capability_effect_calls=5)
    harness = _Harness(config)
    grant = _grant(config, effect_call_limit=3)
    tool = await harness.start(grant=grant, effect_budget=3)

    first = await tool.run(_params(), harness.ctx())
    assert not first.is_error
    # max_reservations is 1, so the capability retires the grant even with budget left.
    assert (await harness.authority.get(grant.id, scope=harness.scope)).status == "consumed"
    second = await tool.run(_params(local_date="2026-09-01"), harness.ctx())
    assert second.is_error


async def test_an_ambiguous_effect_becomes_in_doubt_and_is_never_retried(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)
    grant = _grant(config)
    tool = await harness.start(grant=grant, dispatch=ambiguous_dispatch)

    result = await tool.run(_params(), harness.ctx())

    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "in_doubt"
    assert "Do not retry" in result.content
    # An ambiguous booking exhausts the authority; a second attempt is impossible.
    assert (await harness.authority.get(grant.id, scope=harness.scope)).status == "consumed"
    retry = await tool.run(_params(arrival_time="19:15"), harness.ctx())
    assert retry.is_error
    assert "consumed" in retry.content or "already in_doubt" in retry.content


async def test_a_raising_effect_is_recorded_in_doubt_and_never_replayed(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)
    grant = _grant(config)

    def explode(params: SandboxReserveParams) -> SandboxOutcome:
        del params
        raise RuntimeError("transport connection dropped mid-send")

    tool = await harness.start(grant=grant, dispatch=explode)
    with pytest.raises(RuntimeError):
        await tool.run(_params(), harness.ctx())

    assert (await harness.authority.get(grant.id, scope=harness.scope)).status == "consumed"
    [action] = await harness.jobs.list_actions(scope=harness.scope)
    assert action.status == "in_doubt"
    assert action.grant_id == grant.id
    assert action.task_id == grant.task_id


@pytest.mark.parametrize(
    ("disposition", "phase"),
    [
        (disposition, phase)
        for disposition in ("performed", "in_doubt")
        for phase in (
            "action_resolution",
            "authority_activity",
            "grant_consume",
            "budget_consume",
        )
    ],
)
async def test_delegated_finalization_joins_every_cross_store_phase_before_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: str,
    phase: str,
) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)
    grant = _grant(config)
    dispatch = ambiguous_dispatch if disposition == "in_doubt" else None
    tool = await harness.start(grant=grant, dispatch=dispatch)

    if phase == "action_resolution":
        owner: Any = harness.jobs
        method_name = "resolve_action"
    elif phase == "authority_activity":
        owner = harness.authority
        method_name = "record"
    elif phase == "grant_consume":
        owner = harness.authority
        method_name = "consume"
    else:
        owner = harness.jobs
        method_name = "set_grant_budget_status"

    entered = asyncio.Event()
    release = asyncio.Event()
    original = getattr(owner, method_name)

    async def pause_phase(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, method_name, pause_phase)
    call = asyncio.create_task(tool.run(_params(), harness.ctx()))

    await asyncio.wait_for(entered.wait(), timeout=1)
    call.cancel()
    await asyncio.sleep(0)
    assert not call.done(), "cancellation escaped before delegated finalization joined"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await call

    [action] = await harness.jobs.list_actions(scope=harness.scope)
    assert action.status == disposition
    assert (await harness.authority.get(grant.id, scope=harness.scope)).status == "consumed"
    kinds = [
        item.kind for item in await harness.authority.activities(grant.id, scope=harness.scope)
    ]
    assert kinds == ["issued", "used", "consumed"]
    budget = await harness.jobs.get_grant_budget(grant.id, scope=harness.scope)
    assert budget is not None and budget["status"] == "consumed"


async def test_a_revoked_or_expired_grant_denies_the_next_call(tmp_path: Path) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)
    grant = _grant(config)
    tool = await harness.start(grant=grant)
    await harness.authority.revoke(grant.id, scope=harness.scope, actor="alex", reason="stop")
    await harness.jobs.set_grant_budget_status(grant.id, "revoked", scope=harness.scope)

    result = await tool.run(_params(), harness.ctx())
    assert result.is_error and "revoked" in result.content

    expired_harness = _Harness(settings(tmp_path / "second"))
    expiring = _grant(
        expired_harness.settings,
        expires_at=datetime.now(UTC) + timedelta(milliseconds=1),
    )
    expired_tool = await expired_harness.start(grant=expiring)
    await asyncio.sleep(0.05)
    expired = await expired_tool.run(_params(), expired_harness.ctx())
    assert expired.is_error and "expired" in expired.content


async def test_a_grant_without_a_matching_scope_denies_the_call(tmp_path: Path) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)

    class OtherEvaluator:
        capability = "other_capability"
        schema_id = "other.schema"
        schema_version = 1
        tools = frozenset({"sandbox_reserve"})

        def summarize(self, scope: Any) -> str:  # pragma: no cover
            raise NotImplementedError

        def evaluate_call(self, scope: Any, tool_name: str, args: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

        def effect_identity(self, scope: Any, tool_name: str, args: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

        def receipt(self, scope: Any, result: Any) -> Any:  # pragma: no cover
            raise NotImplementedError

        def consumes_grant(self, scope: Any, receipt: Any) -> bool:  # pragma: no cover
            raise NotImplementedError

    grant = _grant(config)
    tool = await harness.start(
        grant=grant, registry=AuthorityRegistry([cast(Any, OtherEvaluator())])
    )
    result = await tool.run(_params(), harness.ctx())
    assert result.is_error
    assert "no scope for this capability" in result.content


async def test_an_effect_tool_without_an_evaluator_is_not_delegable(tmp_path: Path) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)
    grant = _grant(config)
    tool = await harness.start(grant=grant, registry=AuthorityRegistry())
    result = await tool.run(_params(), harness.ctx())
    assert result.is_error and "no authority evaluator" in result.content


def test_a_grant_cannot_reach_a_tool_the_profile_does_not_expose(tmp_path: Path) -> None:
    config = settings(tmp_path)
    grant = _grant(config)
    delegation = DelegatedRun(
        grant=grant,
        registry=authority_registry(),
        authority=AuthorityStore(config),
    )
    with pytest.raises(DelegatedAuthorityError):
        build_delegated_tools(
            [], delegation, jobs=JobRunStore(config), run_id="jobrun_1", effect_budget=1
        )


def test_a_grant_scope_can_narrow_an_authority_tool_group(tmp_path: Path) -> None:
    config = settings(tmp_path)
    base = authority_registry().require("sandbox_reservation")

    class GroupEvaluator:
        capability = base.capability
        schema_id = base.schema_id
        schema_version = base.schema_version
        tools = frozenset({"sandbox_reserve", "sandbox_cancel"})

        summarize = base.summarize
        evaluate_call = base.evaluate_call
        effect_identity = base.effect_identity
        receipt = base.receipt
        consumes_grant = base.consumes_grant

    constraints = {**COMPLETE_CONSTRAINTS, "allowed_tools": ["sandbox_reserve"]}
    grant = _grant(
        config,
        scopes=(
            AuthorityScope(
                capability="sandbox_reservation",
                schema_id="sandbox.reservation",
                schema_version=1,
                constraints=constraints,
            ),
        ),
    )
    delegation = DelegatedRun(
        grant=grant,
        registry=AuthorityRegistry([cast(Any, GroupEvaluator())]),
        authority=AuthorityStore(config),
    )
    assert delegation.tool_names() == frozenset({"sandbox_reserve"})


def test_an_unattended_forbidden_tool_cannot_join_a_delegated_run(tmp_path: Path) -> None:
    config = settings(tmp_path)
    grant = _grant(config)

    class Destructive(SandboxReservationTool):
        risk = "destructive"
        unattended = "forbidden"

    delegation = DelegatedRun(
        grant=grant,
        registry=authority_registry(),
        authority=AuthorityStore(config),
    )
    with pytest.raises(DelegatedAuthorityError):
        build_delegated_tools(
            [cast(Tool, Destructive(config))],
            delegation,
            jobs=JobRunStore(config),
            run_id="jobrun_1",
            effect_budget=1,
        )
