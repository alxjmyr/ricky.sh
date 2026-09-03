"""Provider protocol for public Web search."""

from __future__ import annotations

from typing import Protocol

from ricky.tools.integrations.web_search.types import WebSearchRequest, WebSearchResponse


class WebSearchProvider(Protocol):
    """Small provider-neutral boundary used by the model-facing tool."""

    async def search(self, request: WebSearchRequest) -> WebSearchResponse:
        """Run exactly one provider request and return canonical evidence."""
        ...

    async def aclose(self) -> None:
        """Close provider-owned resources."""
        ...


__all__ = ["WebSearchProvider"]
