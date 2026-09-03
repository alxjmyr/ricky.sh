"""Canonical Web search contracts and typed settings."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from ricky.config import RickySettings, WebSearchBudgetSettings
from ricky.tools.integrations.web_search.types import (
    WebSearchBudget,
    WebSearchRequest,
    WebSearchResponse,
    WebSearchSource,
)


def _budget_values() -> dict[str, Any]:
    return {
        "candidate_count": 5,
        "source_limit": 3,
        "context_token_limit": 2_048,
        "snippet_limit": 8,
        "tokens_per_source": 1_024,
        "snippets_per_source": 3,
        "result_char_limit": 6_000,
        "relevance_mode": "strict",
    }


def test_canonical_models_survive_json_round_trip() -> None:
    budget = WebSearchBudget(**_budget_values())
    request = WebSearchRequest(
        query="python release",
        country="US",
        search_language="en",
        freshness="week",
        budget=budget,
    )
    source = WebSearchSource(
        title="Python",
        url="https://www.python.org/downloads/?source=ricky",
        hostname="python.org",
        age_labels=["today"],
        snippets=["Python release evidence."],
    )
    response = WebSearchResponse(query=request.query, sources=[source])

    assert WebSearchBudget.model_validate_json(budget.model_dump_json()) == budget
    assert WebSearchRequest.model_validate_json(request.model_dump_json()) == request
    assert WebSearchSource.model_validate_json(source.model_dump_json()) == source
    assert WebSearchResponse.model_validate_json(response.model_dump_json()) == response


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_source_accepts_http_and_https_urls(scheme: str) -> None:
    source = WebSearchSource(
        title="source",
        url=f"{scheme}://example.com/page",
        hostname="example.com",
    )
    assert source.url == f"{scheme}://example.com/page"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/page",
        "file:///tmp/private",
        "javascript:alert(1)",
        "https://example.com/bad\nline",
        "https:///missing-host",
    ],
)
def test_source_rejects_non_public_or_non_line_safe_urls(url: str) -> None:
    with pytest.raises(ValidationError, match="source URL"):
        WebSearchSource(title="bad", url=url, hostname="")


def test_effort_profiles_have_approved_defaults() -> None:
    efforts = RickySettings().web_search.efforts

    assert efforts.quick == WebSearchBudgetSettings(**_budget_values())
    assert efforts.standard.candidate_count == 15
    assert efforts.standard.result_char_limit == 9_500
    assert efforts.deep.candidate_count == 30
    assert efforts.deep.source_limit == 10
    assert efforts.deep.result_char_limit == 11_500


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("candidate_count", 0),
        ("candidate_count", 51),
        ("source_limit", 0),
        ("source_limit", 51),
        ("context_token_limit", 1_023),
        ("context_token_limit", 32_769),
        ("snippet_limit", 0),
        ("snippet_limit", 101),
        ("tokens_per_source", 511),
        ("tokens_per_source", 8_193),
        ("snippets_per_source", 0),
        ("snippets_per_source", 101),
        ("result_char_limit", 999),
        ("result_char_limit", 11_501),
        ("relevance_mode", "lenient"),
    ],
)
def test_effort_profile_rejects_every_absolute_bound(field: str, invalid: Any) -> None:
    values = _budget_values()
    values[field] = invalid
    with pytest.raises(ValidationError):
        WebSearchBudgetSettings(**values)


def test_effort_lookup_is_explicit_and_converts_through_validated_dump() -> None:
    settings = RickySettings()
    selected = settings.web_search.efforts.standard
    canonical = WebSearchBudget.model_validate(selected.model_dump())

    assert isinstance(selected, WebSearchBudgetSettings)
    assert canonical.candidate_count == 15
    assert canonical.relevance_mode == "balanced"


def test_brave_key_environment_alias_is_ignored(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='probe'\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RICKY_BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-secret")

    settings = RickySettings()

    assert settings.brave_search_api_key is None
    assert "brave-secret" not in repr(settings)
