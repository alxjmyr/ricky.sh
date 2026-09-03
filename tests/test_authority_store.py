"""Grant lifecycle and inspectable append-only activity tests."""

from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from authority_support import COMPLETE_CONSTRAINTS, grant_source, settings
from ricky.authority.store import (
    AuthorityStore,
    GrantNotFoundError,
    GrantStateError,
)
from ricky.authority.types import AuthorityScope, DelegationGrant
from ricky.profiles import ProfileScope

SCOPE = ProfileScope.create("personal")


def _grant(*, ttl_seconds: float = 3_600.0, suffix: str = "a") -> DelegationGrant:
    issued = datetime.now(UTC)
    return DelegationGrant(
        id="grant_" + (suffix * 32)[:32],
        source=grant_source(),
        task_id="task_" + "b" * 32,
        task_revision=1,
        profile_scope=SCOPE,
        contract_id="contract_" + "c" * 32,
        contract_digest="d" * 64,
        scopes=(
            AuthorityScope(
                capability="sandbox_reservation",
                schema_id="sandbox.reservation",
                schema_version=1,
                constraints=dict(COMPLETE_CONSTRAINTS),
            ),
        ),
        summary="Make at most one sandbox reservation.",
        effect_call_limit=1,
        issued_at=issued,
        expires_at=issued + timedelta(seconds=ttl_seconds),
        status="active",
        policy_digest="d" * 64,
    )


async def _store(tmp_path: Path) -> AuthorityStore:
    store = AuthorityStore(settings(tmp_path))
    await store.initialize()
    return store


async def test_issue_read_and_list_round_trip(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    grant = await store.issue(_grant(), scope=SCOPE)
    assert await store.get(grant.id, scope=SCOPE) == grant
    assert await store.list(scope=SCOPE, task_id=grant.task_id) == [grant]
    assert await store.list(scope=SCOPE, status="active") == [grant]
    assert await store.list(scope=SCOPE, status="revoked") == []
    with pytest.raises(GrantNotFoundError):
        await store.get("grant_" + "f" * 32, scope=SCOPE)


async def test_issue_records_activity_and_every_use_is_inspectable(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    grant = await store.issue(_grant(), scope=SCOPE)
    await store.record(
        grant.id,
        "used",
        "sandbox_reserve resolved performed",
        scope=SCOPE,
        capability="sandbox_reservation",
        tool_name="sandbox_reserve",
        action_id="action_1",
        disposition="performed",
    )
    await store.record(
        grant.id,
        "denied",
        "sandbox_reserve denied: venue outside scope",
        scope=SCOPE,
    )
    kinds = [item.kind for item in await store.activities(grant.id, scope=SCOPE)]
    assert kinds == ["issued", "used", "denied"]


async def test_revoked_consumed_and_expired_grants_fail_closed(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    revoked = await store.issue(_grant(suffix="a"), scope=SCOPE)
    await store.revoke(revoked.id, scope=SCOPE, actor="alex", reason="changed my mind")
    with pytest.raises(GrantStateError):
        await store.load_active(revoked.id, scope=SCOPE)
    with pytest.raises(GrantStateError):
        await store.revoke(revoked.id, scope=SCOPE, actor="alex", reason="again")

    consumed = await store.issue(_grant(suffix="b"), scope=SCOPE)
    await store.consume(consumed.id, scope=SCOPE, reason="one booking made")
    with pytest.raises(GrantStateError):
        await store.load_active(consumed.id, scope=SCOPE)

    expiring = await store.issue(_grant(suffix="c", ttl_seconds=1), scope=SCOPE)
    with pytest.raises(GrantStateError):
        await store.load_active(
            expiring.id,
            scope=SCOPE,
            now=datetime.now(UTC) + timedelta(minutes=5),
        )
    # Expiry is recorded durably the first time it is observed.
    assert (await store.get(expiring.id, scope=SCOPE)).status == "expired"
    assert [item.kind for item in await store.activities(expiring.id, scope=SCOPE)] == [
        "issued",
        "expired",
    ]


async def test_one_grant_attaches_to_exactly_one_execution_request(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    grant = await store.issue(_grant(), scope=SCOPE)
    request_id = "execution_" + "1" * 32
    attached = await store.attach_execution(grant.id, request_id, scope=SCOPE)
    assert attached.execution_request_id == request_id
    with pytest.raises(GrantStateError):
        await store.attach_execution(grant.id, "execution_" + "2" * 32, scope=SCOPE)


async def test_a_revoked_grant_cannot_take_new_work(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    grant = await store.issue(_grant(), scope=SCOPE)
    await store.revoke(grant.id, scope=SCOPE, actor="alex", reason="stop")
    with pytest.raises(GrantStateError):
        await store.attach_execution(grant.id, "execution_" + "1" * 32, scope=SCOPE)


async def test_a_new_grant_must_be_active(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    with pytest.raises(ValueError):
        await store.issue(_grant().model_copy(update={"status": "revoked"}), scope=SCOPE)


async def test_state_lives_under_user_data_dir_only(tmp_path: Path) -> None:
    config = settings(tmp_path)
    store = AuthorityStore(config)
    await store.initialize()
    await store.issue(_grant(), scope=SCOPE)
    assert store.db_path.is_relative_to(Path(config.user_data_dir).expanduser())
    assert store.db_path.exists()
    assert stat.S_IMODE(store.db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.db_path.parent.stat().st_mode) == 0o700
    project_root = tmp_path / ".ricky"
    assert not (project_root / "authority").exists()


async def test_authority_queries_enforce_profile_scope(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    grant = await store.issue(_grant(), scope=SCOPE)
    work = ProfileScope.create("work")
    cross_profile = ProfileScope.create("work", access_profiles=["personal"])

    with pytest.raises(GrantNotFoundError):
        await store.get(grant.id, scope=work)
    with pytest.raises(GrantNotFoundError):
        await store.revoke(grant.id, scope=work, actor="worker", reason="not authorized")
    assert await store.list(scope=work) == []
    assert await store.list(scope=cross_profile) == [grant]
