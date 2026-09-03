"""Brave LLM Context adapter for canonical Web search contracts."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError

from ricky.tools.integrations.web_search.types import (
    WebSearchRequest,
    WebSearchResponse,
    WebSearchSource,
)

_CONTEXT_PATH = "/res/v1/llm/context"
_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}


class WebSearchError(Exception):
    """Base search failure with text safe to expose to the model."""


class WebSearchApiError(WebSearchError):
    """The search provider rejected or could not complete a request."""


class WebSearchTransportError(WebSearchError):
    """The provider request failed at the network boundary."""


class WebSearchProtocolError(WebSearchError):
    """A successful provider response did not match the expected contract."""


class _BraveGenericSource(BaseModel):
    url: str
    title: str
    snippets: list[str] = Field(default_factory=list)


class _BraveGrounding(BaseModel):
    generic: list[_BraveGenericSource]


class _BraveSourceMetadata(BaseModel):
    hostname: str = ""
    age: list[str] | None = None


class _BraveResponse(BaseModel):
    grounding: _BraveGrounding
    sources: dict[str, _BraveSourceMetadata]


class BraveWebSearchProvider:
    """One-session adapter over Brave's single-search LLM Context endpoint."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        base_url: str,
        timeout_seconds: float,
        read_retry_limit: int,
        max_retry_delay_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._read_retry_limit = read_retry_limit
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            try:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "Content-Type": "application/json",
                        "X-Subscription-Token": self._api_key.get_secret_value(),
                    },
                    transport=self._transport,
                )
            except httpx.InvalidURL as exc:
                raise WebSearchTransportError(
                    "invalid web_search.providers.brave.api_base_url"
                ) from exc
        return self._client

    async def aclose(self) -> None:
        """Close the lazy HTTP client; repeated closes are harmless."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        """Send exactly one successful provider request, plus bounded retries."""
        body = _request_body(request)
        retries = 0
        while True:
            try:
                response = await self._http().post(_CONTEXT_PATH, json=body)
            except httpx.TimeoutException as exc:
                raise WebSearchTransportError(
                    "Web search timed out; the request was not retried because "
                    "completion is unknown."
                ) from exc
            except httpx.HTTPError as exc:
                raise WebSearchTransportError(
                    "Web search transport failed; the request was not retried because "
                    "completion is unknown."
                ) from exc

            retryable = response.status_code == 429 or (500 <= response.status_code < 600)
            if retryable and retries < self._read_retry_limit:
                delay = _retry_delay(
                    response,
                    attempt=retries,
                    maximum=self._max_retry_delay_seconds,
                )
                retries += 1
                await asyncio.sleep(delay)
                continue
            break

        if not 200 <= response.status_code < 300:
            raise _api_error(response.status_code)
        return _parse_response(response, query=request.query)


def _request_body(request: WebSearchRequest) -> dict[str, Any]:
    budget = request.budget
    body: dict[str, Any] = {
        "q": request.query,
        "country": request.country,
        "search_lang": request.search_language,
        "count": budget.candidate_count,
        "maximum_number_of_urls": budget.source_limit,
        "maximum_number_of_tokens": budget.context_token_limit,
        "maximum_number_of_snippets": budget.snippet_limit,
        "maximum_number_of_tokens_per_url": budget.tokens_per_source,
        "maximum_number_of_snippets_per_url": budget.snippets_per_source,
        "context_threshold_mode": budget.relevance_mode,
        "spellcheck": True,
        "enable_local": False,
    }
    if request.freshness != "any":
        body["freshness"] = _FRESHNESS[request.freshness]
    return body


def _parse_response(response: httpx.Response, *, query: str) -> WebSearchResponse:
    try:
        payload = response.json()
        wire = _BraveResponse.model_validate(payload)
        seen: set[str] = set()
        sources: list[WebSearchSource] = []
        for result in wire.grounding.generic:
            if result.url in seen:
                continue
            seen.add(result.url)
            metadata = wire.sources.get(result.url)
            hostname = (
                metadata.hostname
                if metadata is not None and metadata.hostname
                else (urlsplit(result.url).hostname or "")
            )
            sources.append(
                WebSearchSource(
                    title=result.title,
                    url=result.url,
                    hostname=hostname,
                    age_labels=list(metadata.age or []) if metadata is not None else [],
                    snippets=result.snippets,
                )
            )
        return WebSearchResponse(query=query, sources=sources)
    except (ValueError, ValidationError) as exc:
        raise WebSearchProtocolError(
            "Web search provider returned a malformed success response."
        ) from exc


def _api_error(status_code: int) -> WebSearchApiError:
    if status_code in {400, 422}:
        message = "Web search rejected the query or configured retrieval limits."
    elif status_code in {401, 403}:
        message = (
            "Web search authentication or subscription failed; check brave_search_api_key "
            "and Search product access."
        )
    elif status_code == 429:
        message = "Web search rate limit reached; try again shortly."
    else:
        message = f"Web search provider failed with HTTP {status_code}."
    return WebSearchApiError(message)


def _retry_delay(response: httpx.Response, *, attempt: int, maximum: float) -> float:
    if response.status_code == 429:
        resets: list[float] = []
        for raw in response.headers.get("X-RateLimit-Reset", "").split(","):
            try:
                value = float(raw.strip())
            except ValueError:
                continue
            if value > 0:
                resets.append(value)
        delay = min(resets) if resets else 2**attempt
    else:
        delay = 2**attempt
    return min(delay, maximum)


__all__ = [
    "BraveWebSearchProvider",
    "WebSearchApiError",
    "WebSearchError",
    "WebSearchProtocolError",
    "WebSearchTransportError",
]
