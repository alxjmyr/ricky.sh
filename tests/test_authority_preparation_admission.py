"""Do not prompt for effects that durable authority already forbids."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from authority_support import VALID_CALL, authority_registry, settings
from sandbox_support import SandboxReservationTool
from test_authority_engine import _grant, _Harness, _params


@pytest.mark.parametrize("status", ["consumed", "revoked", "expired"])
async def test_unusable_budget_is_rejected_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)
    grant = _grant(config)
    tool = await harness.start(grant=grant)
    await harness.jobs.set_grant_budget_status(grant.id, status, scope=harness.scope)
    prepare = AsyncMock(side_effect=AssertionError("must not ask for approval"))
    dispatch = AsyncMock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(SandboxReservationTool, "prepare_effect", prepare, raising=False)
    monkeypatch.setattr(SandboxReservationTool, "run_prepared", dispatch, raising=False)

    result = await tool.run(_params(), harness.ctx())

    assert result.is_error and status in result.content
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "not_performed"
    prepare.assert_not_called()
    dispatch.assert_not_called()
    budget = await harness.jobs.get_grant_budget(grant.id, scope=harness.scope)
    assert budget is not None and budget["effects_used"] == 0


@pytest.mark.parametrize("loss", ["budget", "authority", "cancellation"])
async def test_authority_lost_during_approval_aborts_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loss: str
) -> None:
    config = settings(tmp_path)
    harness = _Harness(config)
    grant = _grant(config)
    tool = await harness.start(grant=grant)
    identity = (
        authority_registry()
        .require("sandbox_reservation")
        .effect_identity(grant.scopes[0], "sandbox_reserve", dict(VALID_CALL))
    )
    prepared = SimpleNamespace(tool_name=tool.name, identity=identity, permission_summary="Review")

    async def prepare(*_args):
        if loss == "budget":
            await harness.jobs.set_grant_budget_status(grant.id, "revoked", scope=harness.scope)
        elif loss == "authority":
            await harness.authority.consume(grant.id, scope=harness.scope, reason="another effect")
        else:
            monkeypatch.setattr(
                harness.jobs, "get_grant_budget", AsyncMock(side_effect=asyncio.CancelledError)
            )
        return prepared

    dispatch = AsyncMock(side_effect=AssertionError("must not dispatch"))
    abort = AsyncMock()
    monkeypatch.setattr(SandboxReservationTool, "prepare_effect", prepare, raising=False)
    monkeypatch.setattr(SandboxReservationTool, "run_prepared", dispatch, raising=False)
    monkeypatch.setattr(SandboxReservationTool, "abort_prepared", abort, raising=False)

    if loss == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await tool.run(_params(), harness.ctx())
    else:
        result = await tool.run(_params(), harness.ctx())
        assert result.is_error and ("revoked" if loss == "budget" else "consumed") in result.content
        assert result.effect_receipt is not None
        assert result.effect_receipt.disposition == "not_performed"
    dispatch.assert_not_called()
    abort.assert_awaited_once()
    assert abort.call_args.args[0] is prepared
