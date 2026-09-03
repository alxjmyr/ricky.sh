"""Shared authenticated transport core for Google REST integrations.

This module owns the generic call/retry/pagination
machinery shared by every Google service client; each service package keeps
its own tools, error types, and error hints and provides them through the
factory hooks below.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from typing import Any, ClassVar, Protocol

import httpx

READ_RETRY_LIMIT = 2
PAGE_CAP = 5
MAX_RETRY_AFTER_SECONDS = 30.0

QueryValue = str | int | float | bool | None | list[str]


class GoogleAuthLike(Protocol):
    """The slice of GoogleAuth a service client depends on."""

    def validate_account(self, account: str) -> object: ...

    async def get_access_token(
        self,
        account: str,
        *,
        force_refresh: bool = False,
        required_scopes: Collection[str] | None = None,
    ) -> str: ...


class GoogleApiClient:
    """Shared call/retry/pagination core for one authenticated Google service.

    Subclasses set ``service_label``/``config_key``/``default_mutation_check``,
    implement ``_endpoint`` plus the error-factory hooks, and keep any
    service-local caches (labels, timezone) to themselves.
    """

    service_label: ClassVar[str] = "Google"
    config_key: ClassVar[str] = "api_base_url"
    default_mutation_check: ClassVar[str] = "the Google service"

    def __init__(
        self,
        *,
        auth: GoogleAuthLike,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        required_scopes: Collection[str] | None = None,
    ) -> None:
        self._auth = auth
        self._base_url = self._normalize_base_url(base_url)
        self._timeout = timeout_seconds
        self._transport = transport
        self._required_scopes = frozenset(required_scopes) if required_scopes is not None else None
        self._client: httpx.AsyncClient | None = None

    # --- service hooks ----------------------------------------------------

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        return base_url.rstrip("/")

    def _endpoint(self, path: str) -> str:
        return path.lstrip("/")

    def _service_error(self, message: str) -> Exception:
        raise NotImplementedError

    def _api_error(self, *, account: str, status_code: int, reason: str, message: str) -> Exception:
        raise NotImplementedError

    def _transport_error(self, message: str) -> Exception:
        raise NotImplementedError

    def _mutation_unknown_error(self, *, account: str, action: str, check: str) -> Exception:
        raise NotImplementedError

    # --- shared machinery ---------------------------------------------------

    def validate_account(self, account: str) -> None:
        """Fail before network I/O for unknown account names."""
        self._auth.validate_account(account)

    async def call(
        self,
        account: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json_body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        read_only: bool = True,
        mutation_action: str | None = None,
        mutation_check: str | None = None,
    ) -> dict[str, Any]:
        """Call one service endpoint and return a JSON object."""
        self.validate_account(account)
        action = mutation_action or f"{self.service_label} mutation"
        check = mutation_check or self.default_mutation_check
        refreshed = False
        retries = 0
        access_token = await self._access_token(account)

        while True:
            request_headers = {"Authorization": f"Bearer {access_token}"}
            if headers:
                request_headers.update(headers)
            try:
                response = await self._http().request(
                    method,
                    self._endpoint(path),
                    params=params,
                    json=json_body,
                    headers=request_headers,
                )
            except httpx.HTTPError as exc:
                if not read_only:
                    raise self._mutation_unknown_error(
                        account=account, action=action, check=check
                    ) from exc
                raise self._transport_error(
                    f"{self.service_label} read failed for account {account!r}: network error"
                ) from exc

            # A 401 means Google rejected the request without executing it, so
            # one replay after a token refresh stays inside the idempotency
            # boundary even for mutations: the rejected request was not performed.
            if response.status_code == 401 and not refreshed:
                access_token = await self._access_token(account, force_refresh=True)
                refreshed = True
                continue

            error_payload = _response_json_or_none(response)
            reason = _error_reason(error_payload)
            retryable_rate_limit = response.status_code == 429 or (
                response.status_code == 403
                and reason in {"rateLimitExceeded", "userRateLimitExceeded"}
            )
            retryable_server = response.status_code >= 500
            if (
                read_only
                and (retryable_rate_limit or retryable_server)
                and retries < READ_RETRY_LIMIT
            ):
                retries += 1
                await asyncio.sleep(_retry_after_seconds(response))
                continue

            if not read_only and response.status_code >= 500:
                raise self._mutation_unknown_error(account=account, action=action, check=check)

            if response.status_code in {204, 205}:
                return {}

            if error_payload is None:
                if not read_only and response.status_code >= 400:
                    raise self._transport_error(
                        f"{self.service_label} {action} returned non-JSON "
                        f"(HTTP {response.status_code})"
                    )
                raise self._transport_error(
                    f"{self.service_label} read returned non-JSON (HTTP {response.status_code})"
                )

            if response.status_code >= 400:
                error = error_payload.get("error")
                message = str(error.get("message") or "") if isinstance(error, dict) else ""
                raise self._api_error(
                    account=account,
                    status_code=response.status_code,
                    reason=reason,
                    message=message,
                )
            return error_payload

    async def call_paginated(
        self,
        account: str,
        path: str,
        *,
        params: Mapping[str, QueryValue],
        items_key: str,
        page_cap: int = PAGE_CAP,
        max_items: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Follow nextPageToken up to a hard cap; return items and truncation."""
        items: list[dict[str, Any]] = []
        token = ""
        for _ in range(page_cap):
            page_params = dict(params)
            if token:
                page_params["pageToken"] = token
            payload = await self.call(account, "GET", path, params=page_params)
            raw_items = payload.get(items_key) or []
            items.extend(item for item in raw_items if isinstance(item, dict))
            token = str(payload.get("nextPageToken") or "")
            if max_items is not None and len(items) >= max_items:
                return items[:max_items], bool(token or len(items) > max_items)
            if not token:
                return items, False
        return items[:max_items] if max_items is not None else items, True

    async def aclose(self) -> None:
        """Close the service API HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _access_token(self, account: str, *, force_refresh: bool = False) -> str:
        if self._required_scopes is None:
            return await self._auth.get_access_token(account, force_refresh=force_refresh)
        return await self._auth.get_access_token(
            account,
            force_refresh=force_refresh,
            required_scopes=self._required_scopes,
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            try:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout,
                    transport=self._transport,
                )
            except httpx.InvalidURL as exc:
                raise self._service_error(
                    f"invalid {self.config_key} {self._base_url!r}: {exc}"
                ) from exc
        return self._client


def _response_json_or_none(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _error_reason(payload: dict[str, Any] | None) -> str:
    if payload is None:
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors") or []
    if errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason")
        if reason:
            return str(reason)
    return str(error.get("status") or "")


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        delay = float(response.headers.get("Retry-After", "1"))
    except ValueError:
        delay = 1.0
    return max(0.0, min(delay, MAX_RETRY_AFTER_SECONDS))


__all__ = [
    "MAX_RETRY_AFTER_SECONDS",
    "PAGE_CAP",
    "READ_RETRY_LIMIT",
    "GoogleApiClient",
    "GoogleAuthLike",
    "QueryValue",
]
