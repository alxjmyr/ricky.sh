"""Strict lease contracts used by the messaging store."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PollerLease(BaseModel):
    """Exclusive short ownership of one transport account poller."""

    model_config = ConfigDict(extra="forbid")

    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    owner: str = Field(min_length=1, max_length=200)
    token: str = Field(pattern=r"^[0-9a-f]{32}$")
    fence: int = Field(ge=1)
    expires_at: datetime

    @model_validator(mode="after")
    def _validate_expiry(self) -> "PollerLease":
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if self.expires_at.utcoffset() != UTC.utcoffset(self.expires_at):
            raise ValueError("expires_at must use UTC")
        return self
