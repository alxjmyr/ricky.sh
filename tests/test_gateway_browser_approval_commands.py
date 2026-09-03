"""Source-bound gateway browser approval command tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ricky.executions.store import BrowserApprovalError, ExecutionNotFoundError
from ricky.gateway.conversations import ConversationCoordinator
from ricky.gateway.types import Conversation, ConversationKey
from ricky.messaging.types import InboundMessage
from ricky.profiles import ProfileScope

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
_SCOPE = ProfileScope.create("personal")


def _conversation() -> Conversation:
    return Conversation(
        id="conversation_" + "c" * 32,
        key=ConversationKey(
            transport="telegram",
            account="personal/bot",
            destination_id="200",
        ),
        session_id="session_" + "a" * 32,
        route_name="owner",
        provider="openrouter",
        model="test-model",
        profile_scope=_SCOPE,
        status="active",
        revision=4,
        created_at=NOW,
        updated_at=NOW,
    )


def _inbound(text: str, *, suffix: str = "a") -> InboundMessage:
    return InboundMessage(
        id="inbound_" + suffix * 32,
        transport="telegram",
        account="personal/bot",
        update_id=suffix,
        destination_id="200",
        sender_id="100",
        platform_message_id=suffix,
        text=text,
        received_at=NOW,
        status="pending",
    )


class _ApprovalDispatcher:
    def __init__(
        self,
        error: BaseException | None = None,
        *,
        store: object | None = None,
    ) -> None:
        self.error = error
        self.store = store
        self.calls: list[dict[str, object]] = []

    async def decide_browser_approval(
        self,
        approval_id: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        self.calls.append({"approval_id": approval_id, **kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            id=approval_id,
            state="approved" if kwargs["approve"] else "denied",
            request_id="execution_" + "e" * 32,
        )


def _coordinator(*, dispatcher: object | None = None) -> ConversationCoordinator:
    coordinator = object.__new__(ConversationCoordinator)
    if dispatcher is not None:
        coordinator.dispatcher = cast(Any, dispatcher)
    return coordinator


@pytest.mark.parametrize(
    ("verb", "approve", "expected_state", "expected_outcome"),
    [
        ("approve", True, "approved", "will revalidate before commit"),
        ("deny", False, "denied", "will resume without committing"),
    ],
)
async def test_browser_decision_binds_exact_gateway_source(
    verb: str,
    approve: bool,
    expected_state: str,
    expected_outcome: str,
) -> None:
    approval_id = "browser_" + "b" * 32
    dispatcher = _ApprovalDispatcher()
    coordinator = _coordinator(dispatcher=dispatcher)
    inbound = _inbound(f"/{verb} {approval_id} 428193")

    response = await coordinator._browser_decision(  # noqa: SLF001
        inbound.text,
        inbound,
        _conversation(),
        approve=approve,
    )

    assert dispatcher.calls == [
        {
            "approval_id": approval_id,
            "scope": _SCOPE,
            "approve": approve,
            "principal_id": "telegram:personal/bot:100",
            "conversation_id": "conversation_" + "c" * 32,
            "source_message_id": inbound.id,
            "code": "428193",
        }
    ]
    assert expected_state in response
    assert expected_outcome in response
    assert "428193" not in response


@pytest.mark.parametrize("verb", ["approve", "deny"])
async def test_browser_decision_requires_identifier_and_code(verb: str) -> None:
    dispatcher = _ApprovalDispatcher()
    coordinator = _coordinator(dispatcher=dispatcher)
    inbound = _inbound(f"/{verb} browser_" + "b" * 32)

    response = await coordinator._browser_decision(  # noqa: SLF001
        inbound.text,
        inbound,
        _conversation(),
        approve=verb == "approve",
    )

    assert response == f"Usage: /{verb} browser_<approval-id> <one-time-code>"
    assert dispatcher.calls == []


@pytest.mark.parametrize("command", ["/approve-later", "/deny-all"])
async def test_browser_decision_rejects_prefixed_command_names(command: str) -> None:
    dispatcher = _ApprovalDispatcher()
    coordinator = _coordinator(dispatcher=dispatcher)
    inbound = _inbound(f"{command} browser_{'b' * 32} 428193")

    response = await coordinator._browser_decision(  # noqa: SLF001
        inbound.text,
        inbound,
        _conversation(),
        approve=command.startswith("/approve"),
    )

    assert response.startswith("Usage:")
    assert dispatcher.calls == []


@pytest.mark.parametrize(
    "error",
    [
        ExecutionNotFoundError("browser approval not found: browser_secret"),
        BrowserApprovalError("browser approval expired"),
        BrowserApprovalError("browser approval belongs to another principal"),
        BrowserApprovalError("browser approval belongs to another conversation"),
        BrowserApprovalError("browser approval code does not match"),
        BrowserApprovalError("browser approval challenge was already consumed"),
    ],
)
async def test_browser_decision_failures_have_one_non_disclosing_response(
    error: BaseException,
) -> None:
    dispatcher = _ApprovalDispatcher(error)
    coordinator = _coordinator(dispatcher=dispatcher)
    inbound = _inbound(f"/approve browser_{'b' * 32} wrong-code")

    response = await coordinator._browser_decision(  # noqa: SLF001
        inbound.text,
        inbound,
        _conversation(),
        approve=True,
    )

    assert response == (
        "That browser approval could not be applied. Check the exact approval ID "
        "and one-time code from this conversation, then try again."
    )
    assert not any(
        detail in response
        for detail in (
            "browser_secret",
            "expired",
            "principal",
            "another conversation",
            "does not match",
            "already consumed",
        )
    )


class _Sessions:
    async def get(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(revision=7)


@pytest.mark.parametrize("text", ["yes", "Yes.", "NO", "no."])
async def test_bare_yes_or_no_cannot_decide_browser_approval(text: str) -> None:
    dispatcher = _ApprovalDispatcher(store=_StatusStore())
    coordinator = _coordinator(dispatcher=dispatcher)
    coordinator.sessions = cast(Any, _Sessions())

    response, revision = await coordinator._respond(  # noqa: SLF001
        _inbound(text),
        _conversation(),
    )

    assert revision == 7
    assert "/approve" in response
    assert "/deny" in response
    assert dispatcher.calls == []


class _StatusStore:
    async def initialize(self) -> None:
        return None

    async def list_by_conversation(self, *_args: object, **_kwargs: object) -> list[object]:
        return [
            SimpleNamespace(
                id="execution_" + "e" * 32,
                task_id=None,
                status="awaiting_transaction_approval",
            )
        ]

    async def browser_approvals_for_request(
        self, *_args: object, **_kwargs: object
    ) -> list[object]:
        return [
            SimpleNamespace(
                id="browser_" + "b" * 32,
                state="pending",
                expires_at=NOW + timedelta(minutes=15),
                review_digest="d" * 64,
                challenge_digest="secret-challenge-digest",
            )
        ]


async def test_status_shows_safe_awaiting_approval_projection() -> None:
    coordinator = _coordinator(
        dispatcher=SimpleNamespace(store=_StatusStore()),
    )

    response = await coordinator._status(_conversation())  # noqa: SLF001

    assert f"execution_{'e' * 32} awaiting_transaction_approval" in response
    assert f"approval browser_{'b' * 32} pending" in response
    assert "review=dddddddddddd" in response
    assert "expires=" in response
    assert "secret-challenge-digest" not in response
    assert "one-time" not in response
