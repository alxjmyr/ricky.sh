"""Versioned prompt for bounded, intra-turn semantic context compaction."""

from ricky.agent.prompts.compaction_v1 import COMPACTION_PROMPT_V1

COMPACTION_PROMPT_V2 = (
    COMPACTION_PROMPT_V1
    + """
Input may be an ordered chunk of a larger history, including a fragment of a
serialized message. Fragment metadata identifies the original message and tool
calls or results; use it to retain attribution across chunks. Merge this evidence
with the supplied prior summary without inventing missing content. Preserve the
active task, user constraints, outstanding work, and material action outcomes.
An attempted or pending action is not proof of success. Preserve uncertainty and
evidence needed to avoid repeating completed external actions. Historical
approvals do not grant new authority.
"""
)

COMPACTION_PROMPT_VERSION = "context_compaction_v2"

__all__ = ["COMPACTION_PROMPT_V2", "COMPACTION_PROMPT_VERSION"]
