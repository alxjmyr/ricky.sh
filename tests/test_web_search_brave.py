"""Offline wire tests for the Brave LLM Context adapter."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ricky.tools.integrations.web_search.brave import (
    BraveWebSearchProvider,
    WebSearchApiError,
    WebSearchProtocolError,
    WebSearchTransportError,
)
from ricky.tools.integrations.web_search.types import (
    WebSearchBudget,
    WebSearchFreshness,
    WebSearchRequest,
)

BASE = "https://brave.test"
KEY = "brave-super-secret"


def _budget(**overrides: Any) -> WebSearchBudget:
    values: dict[str, Any] = {
        "candidate_count": 5,
        "source_limit": 3,
        "context_token_limit": 2_048,
        "snippet_limit": 8,
        "tokens_per_source": 1_024,
        "snippets_per_source": 3,
        "result_char_limit": 6_000,
        "relevance_mode": "strict",
    }
    values.update(overrides)
    return WebSearchBudget(**values)


def _request(
    *,
    freshness: WebSearchFreshness = "any",
    budget: WebSearchBudget | None = None,
) -> WebSearchRequest:
    return WebSearchRequest(
        query="python 3.14 release",
        country="US",
        search_language="en",
        freshness=freshness,
        budget=budget or _budget(),
    )


def _success() -> dict[str, Any]:
    return {
        "grounding": {
            "generic": [
                {
                    "url": "https://python.org/one",
                    "title": "One",
                    "snippets": ["first", "second"],
                },
                {
                    "url": "https://python.org/one",
                    "title": "Duplicate",
                    "snippets": ["ignored"],
                },
                {
                    "url": "https://docs.python.org/two",
                    "title": "Two",
                    "snippets": ["third"],
                },
            ]
        },
        "sources": {
            "https://python.org/one": {
                "hostname": "python.org",
                "age": ["today", "2026-07-22"],
            }
        },
    }


def _provider(
    handler,
    *,
    retry_limit: int = 1,
    max_delay: float = 2.0,
) -> BraveWebSearchProvider:
    return BraveWebSearchProvider(
        api_key=SecretStr(KEY),
        base_url=BASE,
        timeout_seconds=5,
        read_retry_limit=retry_limit,
        max_retry_delay_seconds=max_delay,
        transport=httpx.MockTransport(handler),
    )


async def test_request_maps_every_budget_field_and_sends_one_post() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"grounding": {"generic": []}, "sources": {}})

    provider = _provider(handler)
    response = await provider.search(_request())
    await provider.aclose()

    assert response.sources == []
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url == f"{BASE}/res/v1/llm/context"
    assert request.headers["x-subscription-token"] == KEY
    assert request.headers["accept"] == "application/json"
    assert request.headers["accept-encoding"] == "gzip"
    body = json.loads(request.content)
    assert body == {
        "q": "python 3.14 release",
        "country": "US",
        "search_lang": "en",
        "count": 5,
        "maximum_number_of_urls": 3,
        "maximum_number_of_tokens": 2_048,
        "maximum_number_of_snippets": 8,
        "maximum_number_of_tokens_per_url": 1_024,
        "maximum_number_of_snippets_per_url": 3,
        "context_threshold_mode": "strict",
        "spellcheck": True,
        "enable_local": False,
    }
    assert KEY not in str(request.url)
    assert KEY not in repr(provider)
    assert KEY not in repr(response)


@pytest.mark.parametrize(
    ("freshness", "wire_value"),
    [("any", None), ("day", "pd"), ("week", "pw"), ("month", "pm"), ("year", "py")],
)
async def test_freshness_mapping(freshness: WebSearchFreshness, wire_value: str | None) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"grounding": {"generic": []}, "sources": {}})

    provider = _provider(handler)
    await provider.search(_request(freshness=freshness))
    await provider.aclose()

    if wire_value is None:
        assert "freshness" not in bodies[0]
    else:
        assert bodies[0]["freshness"] == wire_value


async def test_response_preserves_order_deduplicates_and_joins_metadata() -> None:
    provider = _provider(lambda _request: httpx.Response(200, json=_success()))
    response = await provider.search(_request())
    await provider.aclose()

    assert [source.url for source in response.sources] == [
        "https://python.org/one",
        "https://docs.python.org/two",
    ]
    assert response.sources[0].title == "One"
    assert response.sources[0].hostname == "python.org"
    assert response.sources[0].age_labels == ["today", "2026-07-22"]
    assert response.sources[1].hostname == "docs.python.org"
    assert response.sources[1].age_labels == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"{"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"grounding": {"generic": "wrong"}, "sources": {}}),
    ],
)
async def test_malformed_success_is_protocol_error(response: httpx.Response) -> None:
    provider = _provider(lambda _request: response)
    with pytest.raises(WebSearchProtocolError, match="malformed"):
        await provider.search(_request())
    await provider.aclose()


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (400, "query or configured retrieval limits"),
        (401, "brave_search_api_key"),
        (403, "brave_search_api_key"),
        (422, "query or configured retrieval limits"),
        (429, "rate limit"),
        (503, "HTTP 503"),
    ],
)
async def test_api_errors_are_actionable_and_secret_safe(status: int, message: str) -> None:
    provider = _provider(
        lambda _request: httpx.Response(status, json={"secret_echo": KEY}),
        retry_limit=0,
    )
    with pytest.raises(WebSearchApiError, match=message) as excinfo:
        await provider.search(_request())
    await provider.aclose()

    assert KEY not in str(excinfo.value)
    assert KEY not in repr(excinfo.value)


async def test_429_uses_shortest_positive_reset_and_retries_to_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    waits: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "4, 0.25, 999"})
        return httpx.Response(200, json={"grounding": {"generic": []}, "sources": {}})

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr("ricky.tools.integrations.web_search.brave.asyncio.sleep", fake_sleep)
    provider = _provider(handler, retry_limit=2, max_delay=1.0)
    await provider.search(_request())
    await provider.aclose()

    assert calls == 3
    assert waits == [0.25, 0.25]


async def test_5xx_uses_bounded_exponential_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    waits: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr("ricky.tools.integrations.web_search.brave.asyncio.sleep", fake_sleep)
    provider = _provider(handler, retry_limit=2, max_delay=1.5)
    with pytest.raises(WebSearchApiError, match="503"):
        await provider.search(_request())
    await provider.aclose()

    assert calls == 3
    assert waits == [1.0, 1.5]


async def test_non_5xx_status_above_599_is_not_retried() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(600)

    provider = _provider(handler, retry_limit=3)
    with pytest.raises(WebSearchApiError, match="HTTP 600"):
        await provider.search(_request())
    await provider.aclose()

    assert calls == 1


async def test_transport_timeout_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("secret-free timeout", request=request)

    provider = _provider(handler, retry_limit=3)
    with pytest.raises(WebSearchTransportError, match="not retried"):
        await provider.search(_request())
    await provider.aclose()

    assert calls == 1


async def test_cancellation_during_request_propagates() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    provider = _provider(handler)
    with pytest.raises(asyncio.CancelledError):
        await provider.search(_request())
    await provider.aclose()


async def test_cancellation_during_retry_wait_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancelled_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("ricky.tools.integrations.web_search.brave.asyncio.sleep", cancelled_sleep)
    provider = _provider(lambda _request: httpx.Response(503))
    with pytest.raises(asyncio.CancelledError):
        await provider.search(_request())
    await provider.aclose()


async def test_aclose_is_idempotent_and_client_can_be_recreated() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"grounding": {"generic": []}, "sources": {}})

    provider = _provider(handler)
    await provider.search(_request())
    await provider.aclose()
    await provider.aclose()
    await provider.search(_request())
    await provider.aclose()

    assert calls == 2
