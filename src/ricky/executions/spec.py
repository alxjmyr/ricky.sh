"""Execution-contract budget settings."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ExecutionBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wall_clock_seconds: float = Field(default=600.0, gt=0, le=86_400)
    iterations: int = Field(default=10, ge=1, le=100)
    max_completion_tokens_per_request: int = Field(default=4_096, ge=1, le=1_000_000)
    effect_calls: int = Field(default=0, ge=0, le=1_000)
