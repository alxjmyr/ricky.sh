"""Browser intake preserves empty optional selections as zero authority."""

from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from ricky.authority.types import source_text_digest
from ricky.browser.guardrails import (
    browser_guardrail_constraints,
    browser_guardrail_evaluators,
)
from ricky.capabilities.guardrails import (
    AuthenticatedSource,
    CollectedGuardrailField,
    GuardrailFieldProposal,
)


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
        assert result.question
