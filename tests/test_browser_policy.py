"""Browser destination and provider-disclosure policy tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from ricky.browser.policy import (
    DestinationPolicy,
    canonical_origin,
    provider_safe_url,
    sanitize_aria_snapshot,
)
from ricky.browser.types import BrowserError


def _resolver(*addresses: str) -> Callable[[str, int], Awaitable[tuple[str, ...]]]:
    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return tuple(addresses)

    return resolve


async def test_destination_normalizes_host_port_path_and_fragment() -> None:
    policy = DestinationPolicy(resolver=_resolver("93.184.216.34"))

    result = await policy.validate("HTTPS://ExAmPlE.COM.:443/path?q=one#private")

    assert result.url == "https://example.com/path?q=one"
    assert result.origin == "https://example.com"


@pytest.mark.parametrize(
    "url",
    [
        "about:blank",
        "file:///tmp/private",
        "javascript:alert(1)",
        "https://user:password@example.com/",
        "https:///missing-host",
        "https://example.com/bad\nline",
    ],
)
async def test_destination_rejects_unsafe_or_incomplete_urls(url: str) -> None:
    with pytest.raises(BrowserError) as excinfo:
        await DestinationPolicy(resolver=_resolver("93.184.216.34")).validate(url)

    assert excinfo.value.failure.code == "invalid_destination"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.10.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
    ],
)
async def test_destination_blocks_every_special_address_class(address: str) -> None:
    with pytest.raises(BrowserError) as excinfo:
        await DestinationPolicy(resolver=_resolver(address)).validate("https://example.test/")

    assert excinfo.value.failure.code == "destination_blocked"


async def test_one_special_dns_answer_blocks_the_entire_destination() -> None:
    policy = DestinationPolicy(resolver=_resolver("93.184.216.34", "127.0.0.1"))

    with pytest.raises(BrowserError, match="private or special"):
        await policy.validate("https://example.test/")


async def test_dns_resolution_is_bounded_and_cancels_the_resolver() -> None:
    cancelled = asyncio.Event()

    async def slow_resolver(_host: str, _port: int) -> tuple[str, ...]:
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        raise AssertionError("cancelled resolver unexpectedly resumed")

    policy = DestinationPolicy(
        resolver=slow_resolver,
        resolution_timeout_seconds=0.01,
    )

    with pytest.raises(BrowserError) as timed_out:
        await policy.validate("https://example.test/")

    assert timed_out.value.failure.code == "invalid_destination"
    assert timed_out.value.failure.retryable
    assert cancelled.is_set()


async def test_exact_private_origin_allowance_does_not_widen_to_another_port() -> None:
    policy = DestinationPolicy(
        allowed_private_origins=("http://127.0.0.1:8080",),
        resolver=_resolver("127.0.0.1"),
    )

    allowed = await policy.validate("http://127.0.0.1:8080/page")
    assert allowed.origin == "http://127.0.0.1:8080"

    with pytest.raises(BrowserError) as excinfo:
        await policy.validate("http://127.0.0.1:8081/page")
    assert excinfo.value.failure.code == "destination_blocked"


def test_canonical_origin_handles_defaults_idna_and_ipv6() -> None:
    assert canonical_origin("https://EXAMPLE.com.:443/path") == "https://example.com"
    assert canonical_origin("http://[2001:4860:4860::8888]:8080/") == (
        "http://[2001:4860:4860::8888]:8080"
    )
    assert canonical_origin("https://bücher.example/") == "https://xn--bcher-kva.example"


def test_provider_safe_url_omits_fragment_and_every_query_value() -> None:
    exact = (
        "https://example.com/account?utm_source=private-campaign&token=top-secret"
        "&utm_source=second#payment"
    )

    projected = provider_safe_url(exact)

    assert projected == (
        "https://example.com/account?utm_source=present&token=redacted&utm_source=present"
    )
    assert "private-campaign" not in projected
    assert "top-secret" not in projected
    assert "payment" not in projected
    assert provider_safe_url("about:blank") == "about:blank"
    assert provider_safe_url("not a URL") == "[unavailable]"


def test_snapshot_sanitization_removes_control_values() -> None:
    raw = (
        '- textbox "Card" [ref=e1] [value="4111111111111111"]: 4111111111111111\n'
        '- textbox "Name" [ref=e2] [value=Alex]: Alex\n'
        "  - /value: nested-secret\n"
        '- heading "Public evidence" [ref=e3]'
    )

    sanitized = sanitize_aria_snapshot(raw)

    assert "4111111111111111" not in sanitized
    assert "nested-secret" not in sanitized
    assert "[value=" not in sanitized
    assert 'textbox "Card"' in sanitized
    assert "[ref=e1]" in sanitized
    assert 'heading "Public evidence"' in sanitized


def test_snapshot_sanitization_handles_frame_prefixed_refs() -> None:
    raw = (
        '- textbox "Password" [ref=f1e1]: fixture-password-secret\n'
        '- textbox "Security code" [ref=f1e2]: 123456\n'
        '- textbox "Payment card" [ref=f1e3]: 4111111111111111\n'
        '- heading "Public evidence" [ref=f1e4]'
    )

    sanitized = sanitize_aria_snapshot(raw)

    assert "fixture-password-secret" not in sanitized
    assert "123456" not in sanitized
    assert "4111111111111111" not in sanitized
    assert "[ref=f1e1]" in sanitized
    assert "Public evidence" in sanitized


def test_snapshot_sanitization_removes_spinbutton_values() -> None:
    raw = (
        '- spinbutton "Payment digits" [ref=e1] [value=4111111111111111]: '
        "4111111111111111\n"
        '- heading "Public evidence" [ref=e2]'
    )

    sanitized = sanitize_aria_snapshot(raw)

    assert "4111111111111111" not in sanitized
    assert 'spinbutton "Payment digits" [ref=e1]' in sanitized
    assert "Public evidence" in sanitized


def test_snapshot_sanitization_removes_nested_protected_contenteditable_value() -> None:
    raw = (
        '- textbox "Payment card" [ref=e1] [multiline]\n'
        "  - paragraph [ref=e2]\n"
        "    - text: 4111111111111111\n"
        "  - generic [ref=e3]: security-code-123\n"
        '- textbox "Public notes" [ref=e4]\n'
        "  - paragraph: public draft text\n"
        '- button "Continue" [ref=e5]\n'
    )

    sanitized = sanitize_aria_snapshot(raw, protected_refs={"e1"})

    assert "4111111111111111" not in sanitized
    assert "security-code-123" not in sanitized
    assert 'textbox "Payment card" [ref=e1]' in sanitized
    assert "[multiline]" not in sanitized
    assert "[ref=e2]" not in sanitized
    assert "[ref=e3]" not in sanitized
    assert "public draft text" in sanitized
    assert 'button "Continue" [ref=e5]' in sanitized


def test_snapshot_sanitization_removes_frame_nested_and_inline_protected_values() -> None:
    raw = (
        '- generic "Rich credential editor" [ref=f2e7]: inline-secret\n'
        "  - text: nested-secret\n"
        '- heading "Public evidence" [ref=f2e8]\n'
    )

    sanitized = sanitize_aria_snapshot(raw, protected_refs={"f2e7"})

    assert "inline-secret" not in sanitized
    assert "nested-secret" not in sanitized
    assert sanitized == (
        '- generic "Rich credential editor" [ref=f2e7]\n- heading "Public evidence" [ref=f2e8]\n'
    )
