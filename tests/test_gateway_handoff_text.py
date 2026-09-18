"""Acknowledgement compaction retains every question and failure fact."""

import pytest

from ricky.agent.handoff import BackgroundHandoff, background_handoff_acknowledgement
from ricky.gateway.handoff_text import render_handoff_acknowledgement


def _handoffs(count: int = 1) -> list[BackgroundHandoff]:
    return [
        BackgroundHandoff(request_id=f"execution-{index}", title="Read account balance " * 9)
        for index in range(count)
    ]


def test_short_acknowledgement_is_preserved_and_stripped() -> None:
    handoffs = [BackgroundHandoff(request_id="execution-test", title="Check balance")]
    text = background_handoff_acknowledgement(handoffs)
    assert render_handoff_acknowledgement(handoffs, f"  {text}\n", 200) == text


@pytest.mark.parametrize("count", [1, 2, 25])
def test_long_task_titles_compact_to_complete_task_count(count: int) -> None:
    handoffs = _handoffs(count)
    text = background_handoff_acknowledgement(handoffs)
    noun = "task" if count == 1 else "tasks"
    assert render_handoff_acknowledgement(handoffs, text, 100) == (
        f"I'll run {count} {noun} in the background and report back here."
    )


def test_compaction_preserves_exact_questions_and_failure_facts() -> None:
    handoffs = _handoffs(2)
    suffix = (
        "\n\nWhich account may I use?\n- Personal\n- Work"
        "\n\nOther requests in this turn did not succeed: start_named_job."
    )
    text = background_handoff_acknowledgement(handoffs) + suffix
    rendered = render_handoff_acknowledgement(handoffs, text, 200)
    assert rendered == "I'll run 2 tasks in the background and report back here." + suffix


@pytest.mark.parametrize("suffix", ["", "\n\nPlease confirm this exact boundary: " + "x" * 200])
def test_unrepresentable_acknowledgement_fails_without_truncation(suffix: str) -> None:
    handoffs = _handoffs()
    text = background_handoff_acknowledgement(handoffs) + suffix
    with pytest.raises(ValueError, match="exceeds messaging.body_char_limit; increase the limit"):
        render_handoff_acknowledgement(handoffs, text, 50 if not suffix else 100)


@pytest.mark.parametrize("prefix", ["Unrelated acknowledgement: ", ""])
def test_cannot_compact_unrelated_or_partial_canonical_prefix(prefix: str) -> None:
    handoffs = _handoffs()
    canonical = background_handoff_acknowledgement(handoffs)
    text = prefix + canonical + " appended title text"
    with pytest.raises(ValueError, match="does not match its accepted handoffs"):
        render_handoff_acknowledgement(handoffs, text, 100)
