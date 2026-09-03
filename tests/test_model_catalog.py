"""Tests for canonical model catalogs."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from ricky.config import RickySettings
from ricky.llm.openrouter import OpenRouterProvider
from ricky.llm.types import ModelInfo


async def test_openrouter_model_catalog_maps_canonical_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "provider/model",
                        "name": "Model Name",
                        "context_length": 128000,
                    }
                ]
            },
        )

    settings = RickySettings(openrouter_api_key=SecretStr("test-key"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(settings, client=client, max_retries=1)
        models = await provider.list_models()

    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert models == [
        ModelInfo(
            id="provider/model",
            name="Model Name",
            context_length=128000,
        )
    ]
    assert ModelInfo.model_validate_json(models[0].model_dump_json()) == models[0]


async def test_openrouter_model_catalog_maps_http_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"server error")

    settings = RickySettings(openrouter_api_key=SecretStr("test-key"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenRouterProvider(settings, client=client, max_retries=1)
        try:
            await provider.list_models()
        except Exception as exc:  # noqa: BLE001 - assertion below checks the boundary type.
            error = exc
        else:
            raise AssertionError("expected model catalog failure")

    from ricky.llm.types import TransportError

    assert isinstance(error, TransportError)
