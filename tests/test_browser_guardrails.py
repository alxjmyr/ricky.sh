"""Browser intake preserves empty optional selections as zero authority."""

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from ricky.authority.types import source_text_digest
from ricky.browser.guardrails import (
    BrowserGuardrailConstraints,
    browser_guardrail_constraints,
    browser_guardrail_evaluators,
    compile_browser_execution_scope,
)
from ricky.browser.holds import HOLD_TOOLS
from ricky.capabilities.guardrails import (
    AuthenticatedSource,
    CollectedGuardrailField,
    CompiledGuardrail,
    GuardrailFieldProposal,
    compile_guardrail,
)
from ricky.config import RickySettings
from ricky.executions.browser import BrowserExecutionBudget


@pytest.mark.parametrize("capability_id", ["builtin.browser.read", "builtin.browser.interact"])
@pytest.mark.parametrize("empty", ["", "  \n "], ids=["empty", "whitespace"])
def test_balance_check_empty_optional_selections_match_omission(
    capability_id: str, empty: str
) -> None:
    evaluator = next(
        item for item in browser_guardrail_evaluators() if item.capability_id == capability_id
    )
    text = (
        "Use the personal/ricky-personal browser resource. Go to openrouter.ai "
        "and check my credit balance. Report the balance only."
    )
    source = AuthenticatedSource(
        principal_id="telegram:personal/owner:1",
        conversation_id="conversation_test",
        message_id="inbound_test",
        text_digest=source_text_digest(text),
        text_snapshot=text,
        received_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    selections = {
        "mode": "read_only",
        "allowed_tools": (
            "browser_navigate,browser_snapshot"
            if capability_id == "builtin.browser.read"
            else "browser_session_open_resource"
        ),
        "resources": "personal/ricky-personal",
        "authenticated_origins": "personal/ricky-personal#https://openrouter.ai",
    }
    collected = []
    for name, value in {
        **selections,
        "private_origin_ceiling": empty,
        "attachment_ids": empty,
        "protected_values": empty,
    }.items():
        result = evaluator.normalize_field(GuardrailFieldProposal(field=name, value=value))
        assert result.accepted, result.question
        collected.append(
            CollectedGuardrailField(
                capability_id=capability_id,
                schema_id=evaluator.schema_id,
                schema_version=evaluator.schema_version,
                field=name,
                value=result.value,
                source_message_id=source.message_id,
                source_text_digest=source.text_digest,
            )
        )
    explicit_empty = evaluator.validate_collected(tuple(collected), (source,))
    omitted = evaluator.validate_collected(
        tuple(item for item in collected if item.field in selections), (source,)
    )
    assert not explicit_empty.questions
    assert explicit_empty.guardrail is not None
    assert omitted.guardrail is not None
    constraints = browser_guardrail_constraints(explicit_empty.guardrail)
    assert constraints == browser_guardrail_constraints(omitted.guardrail)
    assert constraints.mode == "read_only"
    assert constraints.authenticated_origins[0].origins == ("https://openrouter.ai",)
    assert not constraints.private_origin_ceiling
    assert not constraints.attachment_ids
    assert not constraints.protected_values


def test_read_oriented_verification_scope_has_no_ordinary_mutation_authority() -> None:
    read = BrowserGuardrailConstraints(
        capability_id="builtin.browser.read",
        mode="read_only",
        allowed_tools=("browser_session_open", "browser_snapshot", "browser_visual_snapshot"),
        allow_ephemeral=True,
        allow_public_https_research=True,
        allow_masked_visual_observations=True,
    )
    verify = BrowserGuardrailConstraints(
        capability_id="builtin.browser.verify",
        mode="read_only",
        allowed_tools=tuple(sorted(HOLD_TOOLS)),
    )

    def compiled(constraints: BrowserGuardrailConstraints) -> CompiledGuardrail:
        return compile_guardrail(
            capability_id=constraints.capability_id,
            schema_id=constraints.capability_id.removeprefix("builtin."),
            schema_version=1,
            constraints=constraints.model_dump(mode="json"),
            sources=(
                AuthenticatedSource(
                    principal_id="owner",
                    conversation_id="conversation_test",
                    message_id="inbound_test",
                    text_digest=source_text_digest("Browse and verify"),
                    text_snapshot="Browse and verify",
                    received_at=datetime(2026, 9, 24, tzinfo=UTC),
                ),
            ),
            summary="Bounded verification in a read-oriented browser",
        )

    budget = BrowserExecutionBudget.model_validate(
        RickySettings().browser.background.budget.model_dump(mode="json"),
        strict=True,
    )
    scope = compile_browser_execution_scope(
        mode="read_only",
        guardrails=(compiled(read), compiled(verify)),
        budget=budget,
    )
    assert set(scope.allowed_tools) >= HOLD_TOOLS
    assert "verification_attempts" in scope.allowed_operations
    assert "interactions" not in scope.allowed_operations
    assert "browser_click" not in scope.allowed_tools
    assert "browser_commit" not in scope.allowed_tools
    with pytest.raises(ValueError, match="authorized visual"):
        compile_browser_execution_scope(
            mode="read_only",
            guardrails=(
                compiled(read.model_copy(update={"allow_masked_visual_observations": False})),
                compiled(verify),
            ),
            budget=budget,
        )


def test_legacy_browser_budget_preserves_pinned_serialization_without_verification() -> None:
    original = RickySettings().browser.background.budget.model_dump(mode="json")
    original.pop("verification_attempts")
    restored = BrowserExecutionBudget.model_validate(original, strict=True)
    assert restored.verification_attempts == 0
    assert restored.model_dump(mode="json") == original


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_tools", ""),
        ("mode", ""),
        ("attachment_ids", "x" * 10_001),
        ("private_origin_ceiling", None),
        ("allow_ephemeral", ""),
    ],
    ids=["required-tools", "required-mode", "oversize", "wrong-type", "boolean"],
)
def test_empty_optional_support_preserves_required_type_and_size_checks(
    field: str, value: JsonValue
) -> None:
    for evaluator in browser_guardrail_evaluators():
        result = evaluator.normalize_field(GuardrailFieldProposal(field=field, value=value))
        assert not result.accepted
        assert result.reason
        assert result.question is None
