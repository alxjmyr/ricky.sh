"""JSON and validation tests for canonical notification contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ricky.notifications import (
    CorrelationRef,
    NotificationRequest,
    OutboxEntry,
    gateway_lifecycle,
)
from ricky.notifications.types import DeliveryAttempt, NotificationRecord, OperatorResolution
from ricky.profiles import ProfileLabel


def test_all_notification_models_survive_json_round_trips() -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    request = NotificationRequest(
        id="notification_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        route="owner",
        title="Ready",
        body="The work is complete.",
        body_format="portable_markdown_v1",
        urgency="normal",
        source_kind="job",
        profile_label=ProfileLabel(required_profiles=("shared", "personal")),
        source_id="hourly-report",
        dedupe_key="completed:run-1",
        correlations=[
            CorrelationRef(
                kind="job_run",
                id="run-1",
                revision=None,
                profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            )
        ],
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    outbox = OutboxEntry(
        id="outbox_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        notification_id=request.id,
        route="owner",
        status="pending",
        attempt_count=0,
        fence=0,
        created_at=now,
        updated_at=now,
    )
    attempt = DeliveryAttempt(
        id=1,
        outbox_id=outbox.id,
        attempt_number=1,
        fence=1,
        worker="worker-1",
        transport="telegram",
        destination_ref="chat-1",
        outcome="delivered",
        started_at=now,
        finished_at=now,
    )
    resolution = OperatorResolution(
        id=1,
        outbox_id=outbox.id,
        disposition="delivered",
        actor="owner",
        created_at=now,
    )
    record = NotificationRecord(request=request, outbox=outbox)

    for model in (request, outbox, attempt, resolution, record):
        assert type(model).model_validate_json(model.model_dump_json()) == model


def test_notification_request_defaults_legacy_json_to_plain_text() -> None:
    request = NotificationRequest.model_validate(
        {
            "id": "notification_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "route": "owner",
            "body": "literal *text*",
            "urgency": "normal",
            "source_kind": "test",
            "profile_label": {"required_profiles": ["shared", "personal"]},
            "source_id": "source",
            "dedupe_key": "legacy",
            "correlations": [],
            "created_at": "2026-08-11T12:00:00Z",
        }
    )

    assert request.body_format == "plain_text"


@pytest.mark.parametrize(
    "kind",
    ["task", "job_run", "execution_request", "workflow_run", "conversation"],
)
def test_every_correlation_requires_profile_label(kind: str) -> None:
    with pytest.raises(ValidationError, match="profile_label"):
        CorrelationRef.model_validate({"kind": kind, "id": "record_1", "revision": 3})


@pytest.mark.parametrize(
    "kind",
    ["task", "job_run", "execution_request", "workflow_run", "conversation"],
)
def test_every_correlation_kind_survives_json_round_trip(kind: str) -> None:
    ref = CorrelationRef.model_validate(
        {
            "kind": kind,
            "id": "record_1",
            "revision": 3,
            "profile_label": {"required_profiles": ["shared", "personal"]},
        }
    )

    assert CorrelationRef.model_validate_json(ref.model_dump_json()) == ref


def test_notification_times_are_aware_utc() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        NotificationRequest(
            id="notification_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            route="owner",
            body="body",
            urgency="normal",
            source_kind="job",
            profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            source_id="job",
            dedupe_key="one",
            correlations=[],
            created_at=datetime(2026, 8, 11),
        )


def test_gateway_lifecycle_request_is_short_and_deduplicated_per_run() -> None:
    request = gateway_lifecycle(
        route="owner",
        run_id="gateway_run_" + "a" * 32,
        state="started",
        profile_label=ProfileLabel(required_profiles=("shared", "personal")),
        created_at=datetime(2026, 8, 14, 12, tzinfo=UTC),
    )

    assert request.route == "owner"
    assert request.title == "Ricky gateway started"
    assert request.body == "The foreground gateway is online."
    assert request.source_kind == "gateway_lifecycle"
    assert request.source_id == "gateway_run_" + "a" * 32
    assert request.dedupe_key == "started"
    assert request.correlations == []
