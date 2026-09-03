"""Serializable contracts for context accounting and inspection."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _ContextContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionModelContext(_ContextContract):
    """Model capacity facts snapshotted when a session is created."""

    context_window_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    image_token_estimate: int = Field(default=8_192, ge=1)
    source: Literal["catalog", "configured", "unknown"] = "unknown"


class ContextSection(_ContextContract):
    """One non-overlapping contribution to a canonical request."""

    name: str
    chars: int = Field(ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    item_count: int = Field(default=0, ge=0)


class ContextBudget(_ContextContract):
    """Hard model-capacity accounting for a request."""

    context_window_tokens: int | None = Field(default=None, ge=1)
    output_reserve_tokens: int = Field(ge=0)
    safety_margin_tokens: int = Field(ge=0)
    hard_input_tokens: int | None = Field(default=None, ge=0)
    remaining_tokens: int | None = None
    capacity_source: Literal["catalog", "configured", "unknown"]


class CheckpointContextReport(_ContextContract):
    """Inspectable provenance for the active history projection."""

    id: str
    created_at: datetime
    covered_raw_messages: int = Field(ge=0)
    retained_raw_messages: int = Field(ge=0)
    original_history_messages: int = Field(ge=0)
    summary_chars: int = Field(ge=0)
    estimated_tokens_before: int = Field(ge=0)
    estimated_tokens_after: int = Field(ge=0)
    estimated_token_reduction: int
    artifact_reference_count: int = Field(ge=0)


class ContextReport(_ContextContract):
    """Complete deterministic accounting for one canonical request."""

    sections: list[ContextSection]
    serialized_chars: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    message_count: int = Field(ge=0)
    tool_count: int = Field(ge=0)
    artifact_count: int = Field(default=0, ge=0)
    stored_artifact_chars: int = Field(default=0, ge=0)
    retained_image_count: int = Field(default=0, ge=0)
    projected_image_count: int = Field(default=0, ge=0)
    projected_image_bytes: int = Field(default=0, ge=0)
    projected_image_pixels: int = Field(default=0, ge=0)
    estimated_image_tokens: int = Field(default=0, ge=0)
    checkpoint: CheckpointContextReport | None = None
    budget: ContextBudget
    pending_user_input_included: bool
