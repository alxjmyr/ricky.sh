"""Provider-neutral contracts for bounded public Web search."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

WebSearchEffort = Literal["quick", "standard", "deep"]
WebSearchFreshness = Literal["any", "day", "week", "month", "year"]
WebSearchRelevance = Literal["strict", "balanced"]


class WebSearchBudget(BaseModel):
    """Hard provider and local rendering limits for one search call."""

    candidate_count: int = Field(ge=1, le=50)
    source_limit: int = Field(ge=1, le=50)
    context_token_limit: int = Field(ge=1_024, le=32_768)
    snippet_limit: int = Field(ge=1, le=100)
    tokens_per_source: int = Field(ge=512, le=8_192)
    snippets_per_source: int = Field(ge=1, le=100)
    result_char_limit: int = Field(ge=1_000, le=11_500)
    relevance_mode: WebSearchRelevance


class WebSearchRequest(BaseModel):
    """One normalized provider-neutral Web search request."""

    query: str
    country: str
    search_language: str
    freshness: WebSearchFreshness
    budget: WebSearchBudget


class WebSearchSource(BaseModel):
    """One ordered public source with provider-extracted evidence."""

    title: str
    url: str
    hostname: str
    age_labels: list[str] = Field(default_factory=list)
    snippets: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        """Accept only complete HTTP(S) URLs while preserving their exact text."""
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("source URL must use http or https")
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("source URL must not contain whitespace or control characters")
        return value


class WebSearchResponse(BaseModel):
    """Ordered evidence returned by a Web search provider."""

    query: str
    sources: list[WebSearchSource] = Field(default_factory=list)


__all__ = [
    "WebSearchBudget",
    "WebSearchEffort",
    "WebSearchFreshness",
    "WebSearchRequest",
    "WebSearchResponse",
    "WebSearchSource",
]
