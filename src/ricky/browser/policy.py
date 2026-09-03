"""Browser navigation and provider-disclosure policy."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ricky.browser.types import BrowserError, BrowserFailure

_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "code",
        "credential",
        "key",
        "otp",
        "password",
        "secret",
        "session",
        "signature",
        "sig",
        "token",
    }
)
_VALUE_ATTRIBUTE = re.compile(r"\s+\[value=(?:\"[^\"]*\"|'[^']*'|[^\]]*)\]")
_FORM_CONTROL_CONTENT = re.compile(
    r"(?m)^(\s*-\s+(?:textbox|searchbox|combobox|spinbutton)\b.*?"
    r"\[ref=(?:f[0-9]+)?e[0-9]+\](?:\s+\[[^\]]+\])*)\s*:.*$"
)
_VALUE_LINE = re.compile(r"(?m)^\s*-\s*/value:.*(?:\n|$)")
_ARIA_REF = re.compile(r"\[ref=(?P<ref>(?:f[0-9]+)?e[0-9]+)\]")


@dataclass(frozen=True)
class ValidatedDestination:
    url: str
    origin: str
    private: bool


Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


async def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    answers = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({str(answer[4][0]) for answer in answers}))


class DestinationPolicy:
    """Validate exact top-level HTTP(S) destinations before browser dispatch."""

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        allowed_private_origins: Iterable[str] = (),
        resolver: Resolver = _system_resolver,
        resolution_timeout_seconds: float = 10.0,
    ) -> None:
        self._allow_private_networks = allow_private_networks
        self._resolver = resolver
        self._resolution_timeout_seconds = resolution_timeout_seconds
        self._allowed_private_origins = frozenset(
            canonical_origin(value) for value in allowed_private_origins
        )

    async def validate(self, url: str) -> ValidatedDestination:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise _invalid_destination("destination is not a valid URL") from exc
        if parts.scheme.lower() not in {"http", "https"}:
            raise _invalid_destination("only http and https destinations are allowed")
        if parts.username is not None or parts.password is not None:
            raise _invalid_destination("URL credentials are not allowed")
        if not parts.hostname:
            raise _invalid_destination("destination must include a host")
        if any(ord(char) < 32 for char in url):
            raise _invalid_destination("destination contains control characters")
        try:
            host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as exc:
            raise _invalid_destination("destination host is invalid") from exc
        if not host:
            raise _invalid_destination("destination must include a host")
        port = port or (443 if parts.scheme.lower() == "https" else 80)
        netloc = _format_netloc(host, port, parts.scheme.lower())
        normalized = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
        origin = canonical_origin(normalized)
        try:
            if _is_ip_literal(host):
                addresses = (host,)
            else:
                async with asyncio.timeout(self._resolution_timeout_seconds):
                    addresses = await self._resolver(host, port)
        except TimeoutError as exc:
            raise BrowserError(
                BrowserFailure(
                    code="invalid_destination",
                    message="destination host resolution timed out",
                    retryable=True,
                )
            ) from exc
        except (OSError, socket.gaierror) as exc:
            raise BrowserError(
                BrowserFailure(
                    code="invalid_destination",
                    message="destination host could not be resolved",
                    retryable=True,
                )
            ) from exc
        if not addresses:
            raise _invalid_destination("destination host did not resolve")
        private = any(_is_special_address(address) for address in addresses)
        if (
            origin not in self._allowed_private_origins
            and not self._allow_private_networks
            and private
        ):
            raise BrowserError(
                BrowserFailure(
                    code="destination_blocked",
                    message="destination resolves to a private or special network address",
                )
            )
        return ValidatedDestination(url=normalized, origin=origin, private=private)


def canonical_origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid origin URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("origin must be an http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("origin cannot contain credentials")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise ValueError("invalid origin host") from exc
    normalized_port = port or (443 if scheme == "https" else 80)
    return f"{scheme}://{_format_netloc(host, normalized_port, scheme)}"


def provider_safe_url(url: str) -> str:
    """Project a URL without fragment or query values."""

    if url == "about:blank":
        return url
    try:
        parts = urlsplit(url)
        origin = canonical_origin(url)
    except ValueError:
        return "[unavailable]"
    keys: list[tuple[str, str]] = []
    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        rendered = "redacted" if key.casefold() in _SENSITIVE_QUERY_NAMES else "present"
        keys.append((key[:200], rendered))
    query = urlencode(keys)
    origin_parts = urlsplit(origin)
    return urlunsplit((origin_parts.scheme, origin_parts.netloc, parts.path or "/", query, ""))


def sanitize_aria_snapshot(
    content: str,
    *,
    protected_refs: Iterable[str] = (),
) -> str:
    """Suppress editable values and protected subtrees from a semantic snapshot.

    Rich ``contenteditable`` controls can expose their value through nested ARIA
    nodes instead of a conventional value attribute.  The backend-provided refs
    let this boundary remove the complete nested subtree while retaining the
    protected control itself as an actionable, opaque target.
    """

    without_attributes = _VALUE_ATTRIBUTE.sub("", content)
    without_inline_values = _FORM_CONTROL_CONTENT.sub(r"\1", without_attributes)
    without_value_lines = _VALUE_LINE.sub("", without_inline_values)
    return _suppress_protected_subtrees(without_value_lines, frozenset(protected_refs))


def _suppress_protected_subtrees(content: str, protected_refs: frozenset[str]) -> str:
    if not protected_refs:
        return content

    rendered: list[str] = []
    protected_indent: int | None = None
    for line in content.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        line_ending = line[len(body) :]
        if protected_indent is not None:
            if not body.strip() or _indent_width(body) > protected_indent:
                continue
            protected_indent = None

        match = _ARIA_REF.search(body)
        if match is None or match.group("ref") not in protected_refs:
            rendered.append(line)
            continue

        # Only the role/name/ref identity is needed after local handoff. Drop
        # everything following the protected ref so an inline value or a
        # page-invented attribute cannot carry the protected value downstream.
        rendered.append(f"{body[: match.end()].rstrip()}{line_ending}")
        protected_indent = _indent_width(body)
    return "".join(rendered)


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _invalid_destination(message: str) -> BrowserError:
    return BrowserError(BrowserFailure(code="invalid_destination", message=message))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_special_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return address.is_multicast or not address.is_global


def _format_netloc(host: str, port: int, scheme: str) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    return rendered_host if port == default_port else f"{rendered_host}:{port}"
