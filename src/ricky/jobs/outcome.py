"""Explicit agent assessment of a task, separate from external-action receipts."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ricky.jobs.types import MAX_RUN_ERROR_CHARS, RunOutcome
from ricky.tools.base import ToolContext, ToolResult


class TaskOutcomeReport(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    status: Literal["completed", "blocked", "uncertain"]
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(min_length=1, max_length=10)

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must describe the task outcome")
        return value

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("each evidence item must contain 1 to 2000 characters")
        return values


class ReportTaskOutcomeTool:
    """Collect a run-local assessment; the runner owns durable terminalization."""

    name = "report_task_outcome"
    description = (
        "Report whether the requested task completed, is blocked, or has an uncertain "
        "outcome. Cite observed results in evidence. A performed browser action, entered "
        "OTP, or dismissed dialog alone does not prove a purchase or login completed. "
        "Use uncertain when a submitted transaction cannot be confirmed. Call after "
        "all inspection and before your final answer. Further tool calls invalidate "
        "this report; report again after further work. Call this tool alone, after "
        "the results of all other tools. This does not authorize retries."
    )
    Params = TaskOutcomeReport
    Result = TaskOutcomeReport
    risk: ClassVar[Literal["read_only"]] = "read_only"
    effect_kind: ClassVar[Literal["none"]] = "none"
    execution_kind: ClassVar[Literal["in_process"]] = "in_process"
    capability_id = None
    unattended = "allowed"
    state_guard_id = None
    timeout_seconds = 5.0

    def __init__(self) -> None:
        self.report: TaskOutcomeReport | None = None
        self._other_tools_in_batch = False
        self._report_attempted = False
        self._reports_in_batch = 0

    def begin_batch(self) -> None:
        self._other_tools_in_batch = False
        self._reports_in_batch = 0

    def tool_requested(self, name: str) -> None:
        self.report = None
        if name == self.name:
            self._report_attempted = True
            self._reports_in_batch += 1
        if name != self.name:
            self._other_tools_in_batch = True

    async def run(self, params: TaskOutcomeReport, ctx: ToolContext) -> ToolResult:
        del ctx
        self._report_attempted = True
        if self._other_tools_in_batch or self._reports_in_batch > 1:
            return ToolResult(
                content=(
                    "Read the other tool results first, then call report_task_outcome "
                    "exactly once in a batch by itself."
                ),
                is_error=True,
            )
        self.report = params.model_copy(deep=True)
        return ToolResult(
            content="Task assessment recorded. Give the user your concise final report.",
            data=params.model_dump(mode="json"),
        )

    def outcome(self, *, required: bool) -> tuple[RunOutcome, str | None]:
        if self.report is None:
            if required or self._report_attempted:
                return "uncertain", "task ended without a current explicit outcome report"
            return "succeeded", None
        if self.report.status == "completed":
            return "succeeded", None
        outcome: RunOutcome = "uncertain" if self.report.status == "uncertain" else "failed"
        error = f"Agent reported task {self.report.status}: {self.report.summary}"
        return outcome, error[:MAX_RUN_ERROR_CHARS]
