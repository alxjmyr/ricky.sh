"""A confirmed commit preserves continuation without refunding any budget."""

from pathlib import Path
from typing import cast

import pytest

from authority_support import settings
from ricky.authority.engine import DelegatedEffectTool
from ricky.authority.registry import AuthorityRegistry
from ricky.browser.authority import browser_authority_evaluators
from ricky.browser.tools import BrowserCommitParams, BrowserTargetParams
from ricky.tools import EffectReceipt, Tool, ToolResult
from test_authority_engine import _grant, _Harness
from test_browser_authority import FINANCIAL, TARGET, _scope


class Effect:
    description = "Synthetic browser dispatch"
    risk = "destructive"
    unattended = "guarded"
    effect_kind = "external"
    state_guard_id = "browser"

    def __init__(self, name: str, disposition: str = "performed") -> None:
        self.name = name
        self.capability_id = (
            "builtin.browser.commit" if name == "browser_commit" else "builtin.browser.interact"
        )
        self.Params = BrowserCommitParams if name == "browser_commit" else BrowserTargetParams
        self.disposition = disposition
        self.calls = 0

    async def run(self, _params, _ctx):
        self.calls += 1
        return ToolResult(
            content="Synthetic dispatch",
            effect_receipt=EffectReceipt.model_validate({"disposition": self.disposition}),
        )


@pytest.mark.parametrize("disposition", ["performed", "in_doubt"])
async def test_post_commit_continuation_keeps_spend_and_effect_reservations(
    tmp_path: Path,
    disposition: str,
) -> None:
    config = settings(
        tmp_path,
        max_effect_calls=5,
        capabilities={
            name: {
                "enabled": True,
                "max_effect_calls": 5,
                "max_financial_limit_minor": 3000 if name == "browser_commit" else 0,
                "currency": "USD" if name == "browser_commit" else None,
                "allowed_profiles": ["shared", "personal"],
            }
            for name in ("browser_commit", "browser_interact")
        },
    )
    harness = _Harness(config)
    grant = _grant(
        config,
        scopes=(
            _scope("browser_commit", "browser_commit"),
            _scope("browser_interact", "browser_click"),
        ),
        effect_call_limit=5,
        financial_limit_minor=3000,
        currency="USD",
    )
    await harness.start(grant=grant)
    registry = AuthorityRegistry(list(browser_authority_evaluators()))

    def wrap(effect: Effect) -> DelegatedEffectTool:
        return DelegatedEffectTool(
            cast(Tool, effect),
            grant=grant,
            registry=registry,
            authority=harness.authority,
            jobs=harness.jobs,
            run_id=harness.run_id,
            effect_budget=5,
        )

    purchase = Effect("browser_commit", disposition)
    purchase_tool = wrap(purchase)
    params = BrowserCommitParams.model_validate({"target": TARGET, "envelope": FINANCIAL})
    result = await purchase_tool.run(params, harness.ctx())
    assert result.effect_receipt and result.effect_receipt.disposition == disposition
    assert purchase.calls == 1
    budget = await harness.jobs.get_grant_budget(grant.id, scope=harness.scope)
    assert budget and budget["financial_used_minor"] == 2000 and budget["effects_used"] == 1

    click = Effect("browser_click")
    result = await wrap(click).run(
        BrowserTargetParams.model_validate({"target": TARGET}), harness.ctx()
    )
    assert click.calls == (1 if disposition == "performed" else 0)
    assert result.is_error == (disposition == "in_doubt")
    active = await harness.authority.get(grant.id, scope=harness.scope)
    assert active.status == ("active" if disposition == "performed" else "consumed")

    # A distinct occurrence cannot reuse the spent financial capacity.
    another = params.model_copy(update={"target": params.target.model_copy(update={"ref": "e2"})})
    denied = await purchase_tool.run(another, harness.ctx())
    assert denied.is_error
    assert purchase.calls == 1
    if disposition == "performed":
        assert "financial limit" in denied.content
    budget = await harness.jobs.get_grant_budget(grant.id, scope=harness.scope)
    assert budget and budget["financial_used_minor"] == 2000
