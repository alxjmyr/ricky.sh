"""Authenticated source construction for contract-bound authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import JsonValue, ValidationError

from authority_support import PRINCIPAL, grant_source, inbound, settings
from ricky.authority.compiler import (
    AuthorityCompilerError,
    ContractAuthorityCompiler,
    build_grant_source,
    principal_id,
)
from ricky.authority.registry import AuthorityRegistry
from ricky.authority.types import GrantSource, source_text_digest
from ricky.browser.authority import (
    BrowserCommitAuthorityEvaluator,
    BrowserInteractAuthorityEvaluator,
)
from ricky.browser.guardrails import BrowserGuardrailConstraints
from ricky.capabilities.guardrails import AuthenticatedSource, compile_guardrail


async def test_a_rejected_message_can_never_source_a_grant() -> None:
    with pytest.raises(AuthorityCompilerError):
        build_grant_source(
            inbound("[rejected update]", status="rejected"),
            conversation_id=grant_source().conversation_id,
            snapshot_chars=2_000,
        )


def test_a_model_or_tool_result_cannot_be_shaped_into_a_source() -> None:
    with pytest.raises(ValidationError):
        GrantSource.model_validate(
            {
                "principal_id": "model",
                "transport": "assistant",
                "account": "assistant",
                "sender_id": "assistant",
                "destination_id": "assistant",
                "platform_message_id": "assistant",
                "inbound_message_id": "tool_result_1",
                "conversation_id": "conversation_" + "a" * 32,
                "text_digest": source_text_digest("please book it"),
                "text_snapshot": "please book it",
                "received_at": "2026-08-13T00:00:00Z",
            }
        )


def test_source_records_exact_identity_digest_and_bounded_snapshot() -> None:
    message = inbound("Book the exact table and retain this tail")
    source = build_grant_source(
        message,
        conversation_id=grant_source().conversation_id,
        snapshot_chars=20,
    )

    assert source.principal_id == PRINCIPAL
    assert source.inbound_message_id == message.id
    assert source.text_digest == source_text_digest(message.text)
    assert source.text_snapshot == message.text[:20]


def test_the_principal_identity_is_transport_account_and_sender() -> None:
    assert principal_id(inbound()) == PRINCIPAL


async def test_browser_commit_grant_binds_the_owner_financial_ceiling(tmp_path) -> None:
    config = settings(
        tmp_path,
        max_effect_calls=3,
        capabilities={
            "browser_interact": {
                "enabled": True,
                "max_effect_calls": 3,
                "allowed_profiles": ["shared", "personal"],
            },
            "browser_commit": {
                "enabled": True,
                "max_effect_calls": 3,
                "max_financial_limit_minor": 25_000,
                "currency": "USD",
                "allowed_profiles": ["shared", "personal"],
            },
        },
    )
    source = grant_source()
    authenticated = AuthenticatedSource(
        principal_id=source.principal_id,
        conversation_id=source.conversation_id,
        message_id=source.inbound_message_id,
        text_digest=source.text_digest,
        text_snapshot=source.text_snapshot,
        received_at=source.received_at,
    )
    constraints = BrowserGuardrailConstraints(
        capability_id="builtin.browser.commit",
        mode="transaction",
        allowed_tools=("browser_commit",),
    )
    guardrail = compile_guardrail(
        capability_id="builtin.browser.commit",
        schema_id="browser.commit",
        schema_version=1,
        constraints=cast(JsonValue, constraints.model_dump(mode="json")),
        sources=(authenticated,),
        summary="Commit one exact separately approved browser transaction.",
    )
    interact_constraints = BrowserGuardrailConstraints(
        capability_id="builtin.browser.interact",
        mode="transaction",
        allowed_tools=("browser_click",),
    )
    interact_guardrail = compile_guardrail(
        capability_id="builtin.browser.interact",
        schema_id="browser.interact",
        schema_version=1,
        constraints=cast(JsonValue, interact_constraints.model_dump(mode="json")),
        sources=(authenticated,),
        summary="Interact with the reviewed browser transaction.",
    )

    class RecordingStore:
        issued = None

        async def initialize(self) -> None:
            pass

        async def issue(self, grant, *, scope) -> None:
            del scope
            self.issued = grant

    store = RecordingStore()
    contract = SimpleNamespace(
        capabilities=(
            SimpleNamespace(
                id="builtin.browser.interact",
                authority_capability="browser_interact",
            ),
            SimpleNamespace(
                id="builtin.browser.commit",
                authority_capability="browser_commit",
            ),
        ),
        profile_scope=config.resolve_profile_scope("personal"),
        tools=(
            *(SimpleNamespace(id=name) for name in BrowserInteractAuthorityEvaluator.tools),
            SimpleNamespace(id="browser_commit"),
            SimpleNamespace(id="browser_coordinate_commit"),
        ),
        guardrails=(interact_guardrail, guardrail),
        budget=SimpleNamespace(effect_calls=3),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        principal_id=source.principal_id,
        source_conversation_id=source.conversation_id,
        source_message_ids=(source.inbound_message_id,),
        task_id="task_" + "a" * 32,
        task_revision=1,
        id="contract_" + "b" * 32,
        digest="c" * 64,
        confirmations=(),
    )
    compiler = ContractAuthorityCompiler(
        config,
        registry=AuthorityRegistry(
            [BrowserInteractAuthorityEvaluator(), BrowserCommitAuthorityEvaluator()]
        ),
        store=cast(Any, store),
    )

    grant = await compiler.compile(cast(Any, contract), source=source)

    assert grant is not None
    assert grant.financial_limit_minor == 25_000
    assert grant.currency == "USD"
    assert store.issued == grant
