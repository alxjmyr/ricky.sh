"""Persistent, file-first memory."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from ricky.memory.types import (
    MemoryCatalogEntry,
    MemoryLoadError,
    MemoryNote,
    MemoryNoteInput,
    NoteType,
)

if TYPE_CHECKING:
    from ricky.memory.store import MemoryStore, memory_note_counts
    from ricky.memory.tools import memory_tools

_LAZY_EXPORTS = {
    "MemoryStore": "ricky.memory.store",
    "memory_note_counts": "ricky.memory.store",
    "memory_tools": "ricky.memory.tools",
}


def __getattr__(name: str) -> Any:
    """Load store and tool exports without creating session/permission cycles."""

    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "MemoryCatalogEntry",
    "MemoryLoadError",
    "MemoryNote",
    "MemoryNoteInput",
    "MemoryStore",
    "NoteType",
    "memory_note_counts",
    "memory_tools",
]
