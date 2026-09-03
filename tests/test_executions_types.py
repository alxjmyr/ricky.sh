"""Boundary-model tests for durable execution requests."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ricky.config import ExecutionSettings
from ricky.executions.types import ExecutionActivity, ExecutionRequest, ExecutionResolution
from ricky.profiles import ProfileScope

SCOPE = ProfileScope.create("personal")


def test_request_activity_and_resolution_survive_json_round_trip() -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    request = ExecutionRequest(
        id="execution_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        kind="ad_hoc",
        status="queued",
        goal="Investigate the issue.",
        contract_id="contract_" + "b" * 32,
        contract_digest="c" * 64,
        task_id="task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        task_revision=3,
        profile_scope=SCOPE,
        notification_route="owner",
        request_key="turn-1",
        created_at=now,
    )
    activity = ExecutionActivity(
        id=1,
        request_id=request.id,
        profile_label=SCOPE.label(),
        kind="submitted",
        to_status="queued",
        summary="submitted",
        fence=0,
        created_at=now,
    )
    resolution = ExecutionResolution(
        id=1,
        request_id=request.id,
        profile_label=SCOPE.label(),
        disposition="confirmed_not_completed",
        actor="owner",
        note="No result was produced.",
        created_at=now,
    )

    for value in (request, activity, resolution):
        assert type(value).model_validate_json(value.model_dump_json()) == value


def test_ad_hoc_request_requires_a_physical_durable_task() -> None:
    with pytest.raises(ValidationError, match="durable task"):
        ExecutionRequest(
            id="execution_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            kind="ad_hoc",
            status="queued",
            goal="Research",
            contract_id="contract_" + "b" * 32,
            contract_digest="c" * 64,
            profile_scope=SCOPE,
            notification_route="owner",
            request_key="turn",
            created_at=datetime.now(UTC),
        )


def test_request_times_must_be_aware() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ExecutionRequest(
            id="execution_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            kind="named_job",
            status="queued",
            named_job="personal/brief",
            job_digest="a" * 64,
            profile_scope=SCOPE,
            notification_route="owner",
            request_key="turn",
            created_at=datetime(2026, 8, 11),
        )


def test_execution_settings_confine_paths_and_lease_timing() -> None:
    with pytest.raises(ValidationError, match="configured root"):
        ExecutionSettings(store_path="../outside.sqlite3")
    with pytest.raises(ValidationError, match="shorter"):
        ExecutionSettings(claim_seconds=10, heartbeat_seconds=10)
