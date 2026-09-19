"""Task assessment never substitutes for action receipts or a clean loop exit."""

from typing import cast

import pytest
from pydantic import ValidationError

from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.jobs.outcome import ReportTaskOutcomeTool, TaskOutcomeReport
from ricky.jobs.runner import JobRunner
from ricky.jobs.store import JobRunStore
from ricky.llm import Message, MessageDone, ToolCallPart
from ricky.tools import Tool, ToolContext
from ricky.tools.testing import assert_tool_contract
from test_jobs_runner_cli import SCOPE, ScriptedProvider, _bundle, _done, _settings


async def test_report_contract_and_json_round_trip(tmp_path):
    settings = RickySettings.model_validate(
        {"user_data_dir": str(tmp_path / "user"), "project_data_dir": str(tmp_path / "project")}
    )
    report = TaskOutcomeReport(
        status="uncertain",
        summary="Purchase pending.",
        evidence=["Checkout shows processing; account balance has not increased."],
    )
    await assert_tool_contract(
        cast(Tool, ReportTaskOutcomeTool()),
        valid_args=report.model_dump(mode="json"),
        ctx=ToolContext(
            settings=settings,
            cwd=tmp_path,
            session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
        ),
    )
    assert TaskOutcomeReport.model_validate_json(report.model_dump_json()) == report
    for evidence in ([], [" "], ["x" * 2001]):
        with pytest.raises(ValidationError):
            TaskOutcomeReport(status="completed", summary="Done", evidence=evidence)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [("completed", "succeeded"), ("blocked", "failed"), ("uncertain", "uncertain")],
)
async def test_explicit_task_outcome(status, outcome):
    reporter = ReportTaskOutcomeTool()
    report = TaskOutcomeReport(status=status, summary="Observed result", evidence=["Page state"])
    result = await reporter.run(report, cast(ToolContext, None))
    assert not result.is_error
    assert reporter.outcome(required=True)[0] == outcome
    if status != "completed":
        await reporter.run(
            report.model_copy(update={"summary": "x" * 2000}), cast(ToolContext, None)
        )
        error = reporter.outcome(required=True)[1]
        assert error is not None and len(error) == 2000


def test_missing_report_cannot_complete_a_browser_transaction():
    reporter = ReportTaskOutcomeTool()
    assert reporter.outcome(required=True)[0] == "uncertain"
    assert reporter.outcome(required=False) == ("succeeded", None)


async def test_report_must_follow_observation_and_is_invalidated_by_later_work():
    reporter = ReportTaskOutcomeTool()
    report = TaskOutcomeReport(status="completed", summary="Done", evidence=["Confirmed page"])
    reporter.begin_batch()
    reporter.tool_requested("browser_snapshot")
    reporter.tool_requested("report_task_outcome")
    result = await reporter.run(report, cast(ToolContext, None))
    assert result.is_error and reporter.report is None
    reporter.begin_batch()
    reporter.tool_requested("report_task_outcome")
    assert not (await reporter.run(report, cast(ToolContext, None))).is_error
    assert reporter.outcome(required=True)[0] == "succeeded"
    reporter.begin_batch()
    reporter.tool_requested("browser_commit")
    assert reporter.outcome(required=True)[0] == "uncertain"
    reporter.begin_batch()
    reporter.tool_requested("report_task_outcome")
    reporter.tool_requested("report_task_outcome")
    assert (await reporter.run(report, cast(ToolContext, None))).is_error
    assert reporter.outcome(required=True)[0] == "uncertain"


@pytest.mark.parametrize("status,expected", [("blocked", "failed"), ("uncertain", "uncertain")])
async def test_job_persists_explicit_outcome_even_when_final_prose_claims_success(
    tmp_path, status, expected
):
    _bundle(tmp_path)
    settings = _settings(tmp_path)
    provider = ScriptedProvider(
        [
            [
                MessageDone(
                    message=Message(
                        role="assistant",
                        content=[
                            ToolCallPart(
                                id="outcome",
                                name="report_task_outcome",
                                args={
                                    "status": status,
                                    "summary": "Result not confirmed",
                                    "evidence": ["The account page has no new transaction."],
                                },
                            )
                        ],
                    )
                )
            ],
            [_done("Success!")],
        ]
    )
    run = await JobRunner(settings, project_root=tmp_path).run(
        "brief", profile_scope=SCOPE, provider=provider
    )
    assert run.outcome == expected
    assert run.error is not None and "Result not confirmed" in run.error
    stored = await JobRunStore(settings).get(run.id, scope=SCOPE)
    assert stored.outcome == expected
