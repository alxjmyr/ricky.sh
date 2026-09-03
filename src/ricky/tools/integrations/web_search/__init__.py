"""Public Web search toolset and provider lifecycle."""

from __future__ import annotations

from ricky.config import RickySettings
from ricky.tools.base import Tool
from ricky.tools.integrations.web_search.brave import WebSearchError
from ricky.tools.integrations.web_search.provider import WebSearchProvider
from ricky.tools.integrations.web_search.tools import WebSearchTool


class WebSearchToolset:
    """The Web search tool plus its selected provider's lifecycle."""

    def __init__(self, provider: WebSearchProvider) -> None:
        self._provider = provider
        self.tools: list[Tool] = [WebSearchTool(provider)]

    async def aclose(self) -> None:
        """Close the selected provider."""
        await self._provider.aclose()


def web_search_toolset(settings: RickySettings) -> WebSearchToolset | None:
    """Build the selected provider, or omit search when its key is absent."""
    provider_name = settings.web_search.provider
    if provider_name != "brave":
        raise ValueError(f"Unknown Web search provider {provider_name!r}; valid providers: brave")
    api_key = settings.brave_search_api_key
    if api_key is None:
        return None
    provider_settings = settings.web_search.providers.brave
    from ricky.tools.integrations.web_search.brave import BraveWebSearchProvider

    provider = BraveWebSearchProvider(
        api_key=api_key,
        base_url=provider_settings.api_base_url,
        timeout_seconds=settings.web_search.request_timeout_seconds,
        read_retry_limit=settings.web_search.read_retry_limit,
        max_retry_delay_seconds=settings.web_search.max_retry_delay_seconds,
    )
    return WebSearchToolset(provider)


__all__ = ["WebSearchError", "WebSearchToolset", "web_search_toolset"]
