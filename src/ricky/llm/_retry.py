"""Shared retry policy for streaming HTTP providers."""

from __future__ import annotations

import random

from ricky.llm.types import ProviderError, RateLimitError, TransportError


def is_retryable(exc: ProviderError) -> bool:
    """Return whether a failure is safe to retry before output is observed."""
    return isinstance(exc, RateLimitError | TransportError)


def retry_delay(base_seconds: float, attempt: int) -> float:
    """Return exponential backoff with bounded jitter."""
    if base_seconds <= 0:
        return 0
    return (base_seconds * (2**attempt)) + random.uniform(0, base_seconds)
