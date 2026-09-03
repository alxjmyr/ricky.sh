"""Shared text/render helpers for integration toolpacks."""

from __future__ import annotations


def cap_text(value: str, limit: int, *, label: str) -> str:
    """Truncate to ``limit`` characters, reserving room for the marker line."""
    if len(value) <= limit:
        return value
    marker = f"\n[... {label} truncated at {limit} chars]"
    return f"{value[: max(0, limit - len(marker))]}{marker}"


def format_size(size: int) -> str:
    """Render a byte count the same way across integrations (e.g. 2KB, 1.5MB)."""
    if size >= 1_048_576:
        return f"{size / 1_048_576:.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size}B"


__all__ = ["cap_text", "format_size"]
