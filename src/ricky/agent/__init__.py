"""Agent core: sessions, events, context assembly, and loop engine."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from ricky.agent.context_types import (
    CheckpointContextReport,
    ContextBudget,
    ContextReport,
    ContextSection,
    SessionModelContext,
)
from ricky.agent.events import (
    AgentErrorEvent,
    AgentEvent,
    ContextAssembledEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
    LlmRequestStartedEvent,
    LlmResponseFinishedEvent,
    PermissionDecidedEvent,
    PermissionRequestedEvent,
    SessionStartedEvent,
    SkillActivatedEvent,
    TasksUpdatedEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallFinishedEvent,
    ToolCallNormalizedEvent,
    ToolCallRejectedEvent,
    ToolCallRequestedEvent,
    ToolCallStartedEvent,
    ToolResultOffloadFailedEvent,
    TurnFinishedEvent,
    TurnStartedEvent,
)
from ricky.agent.session import (
    AgentSession,
    CheckpointObservedState,
    ContextCheckpoint,
    PermissionGrant,
    SessionArtifactRecord,
    TaskItem,
)
from ricky.llm import ToolArtifactRef

if TYPE_CHECKING:
    from ricky.agent.compaction import (
        CompactionRefusedError,
        CompactionSelection,
        ContextCompactor,
        derive_observed_state,
        select_compaction_boundary,
    )
    from ricky.agent.context import ContextAssembly, assemble_context
    from ricky.agent.loop import AgentLoop, PermissionResponder


# Exports that depend on the tool package, loaded lazily to avoid a cycle;
# the submodule names stay reachable as attributes (import ricky.agent;
# ricky.agent.loop) just as the previous eager imports left them.
_LAZY_EXPORTS = {
    "CompactionRefusedError": "ricky.agent.compaction",
    "CompactionSelection": "ricky.agent.compaction",
    "ContextCompactor": "ricky.agent.compaction",
    "derive_observed_state": "ricky.agent.compaction",
    "select_compaction_boundary": "ricky.agent.compaction",
    "ContextAssembly": "ricky.agent.context",
    "assemble_context": "ricky.agent.context",
    "AgentLoop": "ricky.agent.loop",
    "PermissionResponder": "ricky.agent.loop",
}
_LAZY_SUBMODULES = {"compaction", "context", "loop"}


def __getattr__(name: str) -> Any:
    """Load exports that depend on the tool package without creating a cycle."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is not None:
        value = getattr(importlib.import_module(module_name), name)
        globals()[name] = value
        return value
    if name in _LAZY_SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ContextBudget",
    "CheckpointContextReport",
    "CheckpointObservedState",
    "CompactionRefusedError",
    "CompactionSelection",
    "ContextCheckpoint",
    "ContextCompactor",
    "ContextReport",
    "ContextSection",
    "SessionModelContext",
    "LlmResponseFinishedEvent",
    "AgentErrorEvent",
    "AgentEvent",
    "AgentLoop",
    "AgentSession",
    "ContextAssembledEvent",
    "ContextCompactionFailedEvent",
    "ContextCompactionFinishedEvent",
    "ContextCompactionStartedEvent",
    "ContextAssembly",
    "LlmRequestStartedEvent",
    "PermissionDecidedEvent",
    "PermissionGrant",
    "PermissionRequestedEvent",
    "PermissionResponder",
    "SessionStartedEvent",
    "SkillActivatedEvent",
    "TaskItem",
    "TasksUpdatedEvent",
    "TextDeltaEvent",
    "ThinkingDeltaEvent",
    "ToolCallFinishedEvent",
    "ToolCallNormalizedEvent",
    "ToolCallRejectedEvent",
    "ToolCallRequestedEvent",
    "ToolCallStartedEvent",
    "ToolArtifactRef",
    "ToolResultOffloadFailedEvent",
    "TurnFinishedEvent",
    "TurnStartedEvent",
    "SessionArtifactRecord",
    "assemble_context",
    "derive_observed_state",
    "select_compaction_boundary",
]
