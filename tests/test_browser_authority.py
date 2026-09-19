"""Browser authority decodes persisted JSON without weakening strict boundaries."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from authority_support import grant_source, settings
from ricky.authority.store import AuthorityStore
from ricky.authority.types import AuthorityScope, DelegationGrant
from ricky.browser.authority import (
    BrowserCommitAuthorityEvaluator,
    BrowserInteractAuthorityEvaluator,
    ProtectedValueUseAuthorityEvaluator,
)
from ricky.tools.base import EffectReceipt, ToolResult

TARGET = {
    "session_id": "browser_session_" + "a" * 32,
    "page_id": "browser_page_" + "b" * 32,
    "snapshot_id": "browser_snapshot_" + "c" * 32,
    "ref": "e1",
}
FINANCIAL: dict[str, Any] = {
    "kind": "financial",
    "intent": "Buy credits",
    "payee": "OpenRouter",
    "total": {"amount": "20.00", "currency": "USD"},
    "components": [{"label": "Credits", "amount": {"amount": "20.00", "currency": "USD"}}],
    "fees": [],
    "timing": "one_time",
    "source": {"kind": "site", "label": "Saved card ending 1234"},
    "consequences": ["Charge the saved card"],
    "expected_result": "Credits added",
}


def _scope(capability: str, tool: str) -> AuthorityScope:
    schema = {
        "browser_interact": "browser.interact",
        "browser_commit": "browser.commit",
        "protected_value_use": "protected_value.use",
    }[capability]
    resource = {"profile": "personal", "name": "ricky-personal"}
    constraints: dict[str, Any] = {
        "capability_id": "builtin." + schema,
        "mode": "transaction",
        "allowed_tools": [tool],
        "resources": [resource],
        "authenticated_origins": [{"resource": resource, "origins": ["https://openrouter.ai"]}],
        "private_origin_ceiling": [],
        "attachment_ids": [],
        "protected_values": [],
    }
    if capability == "protected_value_use":
        constraints["protected_values"] = [
            {"resource": {"profile": "personal", "name": "card"}, "fields": ["number"]}
        ]
    return AuthorityScope(
        capability=capability, schema_id=schema, schema_version=1, constraints=constraints
    )


async def _persist(tmp_path: Path, authority_scope: AuthorityScope) -> AuthorityScope:
    config = settings(tmp_path)
    scope = config.resolve_profile_scope("personal")
    now = datetime.now(UTC)
    grant = DelegationGrant(
        id="grant_" + "a" * 32,
        source=grant_source(),
        task_id="task_" + "b" * 32,
        task_revision=1,
        profile_scope=scope,
        contract_id="contract_" + "c" * 32,
        contract_digest="d" * 64,
        scopes=(authority_scope,),
        summary="Browser request",
        effect_call_limit=1,
        financial_limit_minor=3000,
        currency="USD",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        status="active",
        policy_digest=config.authority.digest(),
    )
    store = AuthorityStore(config)
    await store.initialize()
    await store.issue(grant, scope=scope)
    # A new store must decode the on-disk grant, not reuse typed in-memory fields.
    reloaded = await AuthorityStore(config).load_active(grant.id, scope=scope)
    return reloaded.scopes[0]


@pytest.mark.parametrize(
    "tool",
    ["browser_click", "browser_fill_protected", "browser_commit", "browser_coordinate_commit"],
)
async def test_persisted_browser_scope_call_identity_and_receipts(
    tmp_path: Path, tool: str
) -> None:
    if tool == "browser_click":
        evaluator = BrowserInteractAuthorityEvaluator()
    elif tool == "browser_fill_protected":
        evaluator = ProtectedValueUseAuthorityEvaluator()
    else:
        evaluator = BrowserCommitAuthorityEvaluator()
    scope = await _persist(tmp_path, _scope(evaluator.capability, tool))
    args: dict[str, Any] = {"target": TARGET}
    if tool == "browser_fill_protected":
        args.update(protected_value="personal/card", field="number")
    elif tool in {"browser_commit", "browser_coordinate_commit"}:
        args["envelope"] = deepcopy(FINANCIAL)
        if tool == "browser_coordinate_commit":
            args["target"] = {
                "session_id": TARGET["session_id"],
                "page_id": TARGET["page_id"],
                "screenshot_id": TARGET["snapshot_id"],
                "x": 10.0,
                "y": 20.0,
            }
    assert tool in evaluator.summarize(scope)
    verdict = evaluator.evaluate_call(scope, tool, args)
    assert verdict.allowed
    assert verdict.amount_minor == (2000 if evaluator.capability == "browser_commit" else 0)
    assert verdict.currency == ("USD" if evaluator.capability == "browser_commit" else None)
    assert evaluator.effect_identity(scope, tool, args) == evaluator.effect_identity(
        scope, tool, deepcopy(args)
    )
    for disposition in ("performed", "not_performed", "in_doubt"):
        receipt = EffectReceipt(disposition=disposition)
        result = ToolResult(content="Effect result", effect_receipt=receipt)
        assert evaluator.receipt(scope, result) == receipt
        assert evaluator.consumes_grant(scope, receipt) == (
            evaluator.capability == "browser_commit" and disposition != "not_performed"
        )
    assert (
        evaluator.receipt(scope, ToolResult(content="Failed", is_error=True)).disposition
        == "not_performed"
    )
    assert evaluator.receipt(scope, ToolResult(content="No evidence")).disposition == "in_doubt"
    assert not evaluator.evaluate_call(scope, "browser_download", args).allowed
    if tool == "browser_fill_protected":
        args["field"] = "security_code"
        assert not evaluator.evaluate_call(scope, tool, args).allowed
    else:
        args["target"] = {**args["target"], "unexpected": True}
        assert not evaluator.evaluate_call(scope, tool, args).allowed


@pytest.mark.parametrize(
    "field,value",
    [("allowed_tools", [123]), ("allow_ephemeral", "false"), ("extra", True), ("version", "1")],
)
async def test_persisted_scope_still_rejects_malformed_constraints(
    tmp_path: Path, field: str, value: Any
) -> None:
    scope = _scope("browser_interact", "browser_click")
    assert isinstance(scope.constraints, dict)
    constraints = {**scope.constraints, field: value}
    scope = await _persist(tmp_path, scope.model_copy(update={"constraints": constraints}))
    with pytest.raises(ValidationError):
        BrowserInteractAuthorityEvaluator().summarize(scope)


@pytest.mark.parametrize("change", ["numeric_amount", "string_array", "wrong_currency", "extra"])
async def test_persisted_commit_scope_rejects_invalid_nested_envelope(
    tmp_path: Path, change: str
) -> None:
    scope = await _persist(tmp_path, _scope("browser_commit", "browser_commit"))
    envelope = deepcopy(FINANCIAL)
    if change == "numeric_amount":
        envelope["total"]["amount"] = 20
    elif change == "string_array":
        envelope["consequences"] = "Charge card"
    elif change == "wrong_currency":
        envelope["components"][0]["amount"]["currency"] = "EUR"
    else:
        envelope["source"]["extra"] = True
    verdict = BrowserCommitAuthorityEvaluator().evaluate_call(
        scope, "browser_commit", {"target": TARGET, "envelope": envelope}
    )
    assert not verdict.allowed


async def test_nonfinancial_commit_and_exact_financial_identity_survive_persistence(
    tmp_path: Path,
) -> None:
    scope = await _persist(tmp_path, _scope("browser_commit", "browser_commit"))
    evaluator = BrowserCommitAuthorityEvaluator()
    nonfinancial = {
        "kind": "browser",
        "intent": "Send message",
        "destination": "https://openrouter.ai",
        "consequences": ["Send support request"],
        "disclosures": ["Account name"],
        "expected_result": "Support request submitted",
    }
    verdict = evaluator.evaluate_call(
        scope, "browser_commit", {"target": TARGET, "envelope": nonfinancial}
    )
    assert verdict.allowed
    assert verdict.amount_minor == 0
    assert verdict.currency is None
    original = evaluator.effect_identity(
        scope, "browser_commit", {"target": TARGET, "envelope": FINANCIAL}
    )
    changed = deepcopy(FINANCIAL)
    changed["total"]["amount"] = "21.00"
    changed["fees"] = [{"label": "Fee", "amount": {"amount": "1.00", "currency": "USD"}}]
    assert (
        evaluator.evaluate_call(
            scope, "browser_commit", {"target": TARGET, "envelope": changed}
        ).amount_minor
        == 2100
    )
    assert (
        evaluator.effect_identity(scope, "browser_commit", {"target": TARGET, "envelope": changed})
        != original
    )
