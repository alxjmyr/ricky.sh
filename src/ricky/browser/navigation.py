"""Strict local projection of Chrome's native navigation pause events."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class NavigationHeader(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: str
    value: str


class NavigationPause(BaseModel):
    """Keep only the protocol facts used by navigation enforcement."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    request_id: str
    frame_id: str
    url: str
    redirected_request_id: str | None
    status: int | None
    response_error: str | None
    headers: tuple[NavigationHeader, ...]

    @property
    def is_response(self) -> bool:
        return self.status is not None or self.response_error is not None

    def header(self, name: str) -> str:
        return next((item.value for item in self.headers if item.name.lower() == name), "")

    @classmethod
    def from_protocol(cls, payload: dict[str, object]) -> NavigationPause:
        request = payload["request"]
        if not isinstance(request, dict):
            raise ValueError("invalid Chrome navigation request")
        headers = payload.get("responseHeaders", [])
        if not isinstance(headers, list):
            raise ValueError("invalid Chrome navigation response headers")
        return cls.model_validate(
            {
                "request_id": payload["requestId"],
                "frame_id": payload["frameId"],
                "url": request["url"],
                "redirected_request_id": payload.get("redirectedRequestId"),
                "status": payload.get("responseStatusCode"),
                "response_error": payload.get("responseErrorReason"),
                "headers": tuple(NavigationHeader.model_validate(header) for header in headers),
            }
        )
