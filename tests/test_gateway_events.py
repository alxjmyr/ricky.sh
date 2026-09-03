"""Safe terminal and service-file gateway event rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

from ricky.agent.events import (
    ToolCallFinishedEvent,
    ToolCallNormalizedEvent,
    ToolCallRejectedEvent,
    ToolCallRequestedEvent,
    TurnStartedEvent,
)
from ricky.gateway.service import ServiceEvent
from ricky.interfaces.cli.gateway_events import GatewayEventRenderer

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


class FlushingStream(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def test_renderer_projects_safe_metadata_and_flushes_every_line() -> None:
    stream = FlushingStream()
    renderer = GatewayEventRenderer(stream)

    renderer.render_service(
        ServiceEvent(
            kind="claim",
            loop="inbox",
            at=NOW,
            record_id="inbound_x",
            summary="claiming inbound message",
        )
    )
    renderer.render_agent(
        TurnStartedEvent(turn_id="turn_x", user_input="PRIVATE USER TEXT", timestamp=NOW)
    )
    renderer.render_agent(
        ToolCallRequestedEvent(
            turn_id="turn_x",
            call_id="call_x",
            tool_name="read_durable_task",
            args={"task_id": "SECRET TOOL ARG"},
            timestamp=NOW,
        )
    )
    renderer.render_agent(
        ToolCallFinishedEvent(
            turn_id="turn_x",
            call_id="call_x",
            tool_name="read_durable_task",
            is_error=False,
            content_chars=19,
            content="PRIVATE TOOL RESULT",
            timestamp=NOW,
        )
    )

    text = stream.getvalue()
    assert "2026-08-14T12:00:00+00:00 gateway claim inbox record=inbound_x" in text
    assert "agent turn_started turn_x: started" in text
    assert "requested tool=read_durable_task call=call_x" in text
    assert "finished tool=read_durable_task call=call_x outcome=ok" in text
    assert "PRIVATE USER TEXT" not in text
    assert "SECRET TOOL ARG" not in text
    assert "PRIVATE TOOL RESULT" not in text
    assert stream.flushes == 4


def test_renderer_projects_normalization_and_rejection_without_argument_values() -> None:
    stream = FlushingStream()
    renderer = GatewayEventRenderer(stream)

    renderer.render_agent(
        ToolCallNormalizedEvent(
            turn_id="turn_x",
            call_id="call_y",
            tool_name="gmail_send_message",
            paths=["attachments.0"],
            timestamp=NOW,
        )
    )
    renderer.render_agent(
        ToolCallRejectedEvent(
            turn_id="turn_x",
            call_id="call_z",
            tool_name="gmail_send_message",
            reason="invalid_arguments",
            repairable=True,
            external_effect=True,
            input_digest="a" * 64,
            timestamp=NOW,
        )
    )

    text = stream.getvalue()
    assert "normalized tool=gmail_send_message call=call_y paths=1" in text
    assert "rejected tool=gmail_send_message call=call_z reason=invalid_arguments" in text
    assert "attachments.0" not in text
    assert "a" * 64 not in text
    assert stream.flushes == 2
