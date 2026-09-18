"""Bound gateway acknowledgement text without dropping accepted work or questions."""

from collections.abc import Sequence

from ricky.agent.handoff import BackgroundHandoff, background_handoff_acknowledgement


def render_handoff_acknowledgement(
    handoffs: Sequence[BackgroundHandoff], acknowledgement: str, max_chars: int
) -> str:
    """Preserve exact continuation text while compacting only the canonical task list."""
    rendered = acknowledgement.strip()
    if len(rendered) <= max_chars:
        return rendered
    canonical = background_handoff_acknowledgement(handoffs)
    if not rendered.startswith(canonical):
        raise ValueError("background acknowledgement does not match its accepted handoffs")
    suffix = rendered[len(canonical) :]
    # The canonical prefix must end at its message boundary, not inside an
    # unrelated title or sentence that happens to begin with the same text.
    if suffix and not suffix.startswith("\n\n"):
        raise ValueError("background acknowledgement does not match its accepted handoffs")
    count = len(handoffs)
    noun = "task" if count == 1 else "tasks"
    compact = f"I'll run {count} {noun} in the background and report back here.{suffix}"
    if len(compact) > max_chars:
        raise ValueError(
            "background acknowledgement exceeds messaging.body_char_limit; increase the limit"
        )
    return compact
