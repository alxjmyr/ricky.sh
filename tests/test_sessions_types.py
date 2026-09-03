"""Persistent-session contract tests."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.llm import Message
from ricky.sessions import SessionLease, StoredSession, StoredTurn


def test_session_models_survive_json_round_trip() -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [Message.text("user", "hello"), Message.text("assistant", "hi")]
    stored = StoredSession(
        session=session,
        profile_label=session.profile_scope.label(),
        revision=3,
        status="active",
        created_at=now,
        updated_at=now,
        last_turn_id="turn_one",
    )
    lease = SessionLease(
        session_id=session.id,
        profile_label=session.profile_scope.label(),
        owner="test-worker",
        token="lease_one",
        fence=2,
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    turn = StoredTurn(
        id="turn_one",
        session_id=session.id,
        profile_label=session.profile_scope.label(),
        inbound_ref="cli:one",
        base_revision=2,
        status="committed",
        started_at=now,
        finished_at=now + timedelta(seconds=1),
    )

    assert StoredSession.model_validate_json(stored.model_dump_json()) == stored
    assert SessionLease.model_validate_json(lease.model_dump_json()) == lease
    assert StoredTurn.model_validate_json(turn.model_dump_json()) == turn


def test_models_reject_unknown_fields_and_naive_times() -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    settings = RickySettings()
    settings = RickySettings()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SessionLease.model_validate(
            {
                "session_id": "session_one",
                "owner": "worker",
                "token": "lease_one",
                "fence": 1,
                "acquired_at": now,
                "expires_at": now + timedelta(seconds=1),
                "surprise": True,
            }
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        StoredTurn(
            id="turn_one",
            session_id="session_one",
            profile_label=settings.resolve_profile_scope().label(),
            base_revision=0,
            status="running",
            started_at=datetime(2026, 8, 11, 12),
        )
