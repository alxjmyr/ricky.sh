"""Rich renderer for the CLI event stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from rich.console import Console, RenderableType
from rich.json import JSON
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.table import Table
from rich.text import Text

from ricky.agent.context_types import ContextReport
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
    UserInteractionRequiredEvent,
    WorkflowEvent,
)
from ricky.agent.session import AgentSession, PermissionGrant
from ricky.agent.workflow import ApprovalRequest, ApprovalResponse
from ricky.config import RickySettings, user_data_path
from ricky.interfaces.cli.input import CHAT_COMMANDS, CliInputSession
from ricky.memory.types import MemoryLoadError
from ricky.permissions import PermissionResponse
from ricky.protected_values import (
    DestinationApprovalRequest,
    DestinationApprovalResponse,
    SecureValueInputRequest,
    UnlockRequest,
)
from ricky.skills.registry import SkillRegistry
from ricky.skills.spec import SkillLoadError
from ricky.workflows.registry import WorkflowLoadError, WorkflowRegistry


class CliRenderer:
    """Render agent events and CLI prompts with Rich."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        debug: bool = False,
        input_session: CliInputSession | None = None,
    ) -> None:
        self.console = console or Console()
        self.debug = debug
        self.input_session = input_session or CliInputSession(self.console)
        self._assistant_buffer: list[str] = []
        self._activity: Status | None = None
        self._activity_message: str | None = None
        self._active_tools: dict[str, str] = {}
        # A workflow may keep scheduling other steps while one step waits for
        # a human decision.  Terminal input and event output share stdout, so
        # only one prompt may own it and all events must wait until that prompt
        # has released the line.
        self._prompt_lock = asyncio.Lock()
        self._prompt_active = False
        self._deferred_events: list[AgentEvent] = []

    async def read_user_input(self) -> str:
        """Read one possibly multiline prompt without blocking the event loop."""
        return await self.input_session.read_chat()

    def configure_chat_input(self, skill_registry: SkillRegistry) -> None:
        """Expose only top-level slash commands and skills to completion."""
        self.input_session.configure_completion(
            commands=CHAT_COMMANDS,
            skills=skill_registry.identifiers(),
        )

    async def request_permission(self, event: PermissionRequestedEvent) -> PermissionResponse:
        """Prompt the user for a permission response."""
        async with self._interactive_prompt():
            self.finish_stream()
            summary = event.summary or summarize_tool_call(event.tool_name, event.args)
            body = Text()
            body.append(event.tool_name, style="bold")
            if summary:
                body.append(f"\n{summary}")
            body.append(f"\nReason: {event.reason}", style="dim")

            # The loop decides *what* may be remembered and puts it on the event; the
            # renderer only assigns a key to each offered option. Keys are
            # case-sensitive so "a" (scoped) and "A" (whole tool) stay distinct;
            # directory grants use their own lower-case key.
            key_by_id = {"scoped": "a", "directory": "d", "tool": "A"}
            grant_by_key: dict[str, str] = {}
            for option in event.offered_grants:
                key = key_by_id.get(option.id)
                if key is None:
                    continue
                grant_by_key[key] = option.id
                body.append(f"\n{key} = {option.label}", style="dim")
            self.console.print(Panel(body, title="Permission required", border_style="yellow"))

            keys = ["y", *grant_by_key.keys(), "n"]
            prompt = f"[bold yellow]Allow[/bold yellow] [{'/'.join(keys)}] (n): "
            while True:
                try:
                    answer = (await self._read_prompt_line(prompt)).strip()
                except EOFError:
                    answer = ""
                if answer in {"y", "Y"}:
                    return PermissionResponse(decision="allow")
                if answer in {"n", "N", ""}:
                    return PermissionResponse(decision="deny")
                if answer in grant_by_key:
                    return PermissionResponse(decision="allow", grant=grant_by_key[answer])
                self.render_status(f"Choose one of {', '.join(keys)}.", style="yellow")

    async def request_workflow_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        """Prompt for one confirmation or stable-key collection selection."""
        async with self._interactive_prompt():
            self.finish_stream()
            if request.mode == "confirm":
                body = Text(request.prompt)
                body.append(
                    "\n" + json.dumps(request.proposal, ensure_ascii=False, indent=2),
                    style="dim",
                )
                self.console.print(
                    Panel(
                        body,
                        title=f"Workflow approval · {request.step_id}",
                        border_style="yellow",
                    )
                )
                try:
                    answer = (
                        (
                            await self._read_prompt_line(
                                "[bold yellow]Approve[/bold yellow] [y/n] (n): "
                            )
                        )
                        .strip()
                        .lower()
                    )
                except EOFError:
                    answer = ""
                return ApprovalResponse(approved=answer in {"y", "yes"})

            from ricky.interfaces.cli.select import select_subset

            labels = [
                f"{key}: {json.dumps(item, ensure_ascii=False, sort_keys=True)}"
                for key, item in zip(request.item_keys, request.collection, strict=True)
            ]
            indexes = await select_subset(labels, self, title=request.prompt)
            return ApprovalResponse(
                approved=bool(indexes),
                selected_keys=[request.item_keys[index] for index in indexes],
            )

    @asynccontextmanager
    async def _interactive_prompt(self) -> AsyncIterator[None]:
        """Give one interactive prompt exclusive ownership of terminal output."""
        async with self._prompt_lock:
            self._prompt_active = True
            try:
                yield
            finally:
                self._prompt_active = False
                self._flush_deferred_events()

    async def preview_and_deny_workflow_approval(
        self, request: ApprovalRequest
    ) -> ApprovalResponse:
        """Show a dry-run review surface and return the fail-closed decision."""

        self.finish_stream()
        if request.mode == "confirm":
            body = Text(request.prompt)
            body.append(
                "\n" + json.dumps(request.proposal, ensure_ascii=False, indent=2),
                style="dim",
            )
            self.console.print(
                Panel(
                    body,
                    title=f"Dry-run approval · {request.step_id}",
                    border_style="yellow",
                )
            )
        elif request.collection:
            lines = [
                f"{key}: {json.dumps(item, ensure_ascii=False, sort_keys=True)}"
                for key, item in zip(request.item_keys, request.collection, strict=True)
            ]
            self.console.print(
                Panel(
                    "\n".join(lines),
                    title=request.prompt,
                    border_style="yellow",
                )
            )
        else:
            self.render_status(f"{request.prompt} — nothing to review.", style="dim")
        self.render_status("Dry run auto-denied this approval.", style="yellow")
        return ApprovalResponse()

    async def read_line(self, prompt: str) -> str:
        """Read one line with an arbitrary prompt without blocking the loop."""
        return await self._read_prompt_line(prompt)

    async def read_secret(self, prompt: str) -> SecretStr | None:
        """Read one local no-echo value while exclusively owning terminal input."""
        async with self._interactive_prompt():
            self.finish_stream()
            try:
                value = await self.input_session.read_secret(prompt)
            except (EOFError, KeyboardInterrupt):
                return None
        return SecretStr(value) if value else None

    async def request_protected_unlock(self, request: UnlockRequest) -> SecretStr | None:
        """Prompt locally for one profile vault passphrase."""
        return await self.read_secret(f"Unlock protected values for profile {request.profile}: ")

    async def request_secure_value(self, request: SecureValueInputRequest) -> SecretStr | None:
        """Collect one prompt-each-use value without rendering it."""
        return await self.read_secret(
            f"Enter {request.label} for {request.ref.qualified} on {request.top_level_origin}: "
        )

    async def request_protected_destination(
        self, request: DestinationApprovalRequest
    ) -> DestinationApprovalResponse:
        """Review an exact protected-value destination pair locally."""
        async with self._interactive_prompt():
            self.finish_stream()
            body = Text()
            body.append("Protected value: ")
            body.append(request.ref.qualified)
            body.append(" (")
            body.append(request.label)
            body.append(")\nTop-level origin: ")
            body.append(request.top_level_origin)
            body.append("\nTarget-frame origin: ")
            body.append(request.frame_origin)
            self.console.print(
                Panel(
                    body,
                    title="New protected-value destination",
                    border_style="yellow",
                )
            )
            try:
                answer = (
                    (
                        await self._read_prompt_line(
                            "[bold yellow]Allow once / approve / deny[/bold yellow] [o/a/n] (n): "
                        )
                    )
                    .strip()
                    .lower()
                )
            except (EOFError, KeyboardInterrupt):
                answer = ""
        decision = "allow_once" if answer == "o" else "approve" if answer == "a" else "deny"
        return DestinationApprovalResponse(decision=decision)

    async def _read_prompt_line(self, prompt: str) -> str:
        """Read one single-line answer using the current input backend."""
        self.finish_stream()
        return await self.input_session.read_line(prompt)

    def render_event(self, event: AgentEvent) -> None:
        """Render one agent event."""
        if self._prompt_active:
            self._deferred_events.append(event)
            return
        if isinstance(event, TextDeltaEvent):
            self._assistant_buffer.append(event.delta)
            return

        if isinstance(event, LlmRequestStartedEvent):
            self._print_event_renderable(event)
            self._set_activity("Thinking…")
            return
        if isinstance(event, LlmResponseFinishedEvent):
            self._stop_activity()
            self._render_assistant_buffer(partial=False)
            self._print_event_renderable(event)
            return
        if isinstance(event, ToolCallStartedEvent):
            self._print_event_renderable(event)
            self._active_tools[event.call_id] = event.tool_name
            self._refresh_tool_activity()
            return
        if isinstance(event, ToolCallFinishedEvent):
            self._stop_activity()
            self._active_tools.pop(event.call_id, None)
            self._print_event_renderable(event)
            self._refresh_tool_activity()
            return
        if isinstance(event, (TurnFinishedEvent, AgentErrorEvent)):
            self._stop_activity()
            self._active_tools.clear()
            self._render_assistant_buffer(partial=True)

        self._print_event_renderable(event)

    def _print_event_renderable(self, event: AgentEvent) -> None:
        """Print one event's renderable without changing activity state."""
        renderable = self.renderable_for(event)
        if renderable is not None:
            self.console.print(renderable)

    def _flush_deferred_events(self) -> None:
        """Render events received while an interactive prompt owned stdout."""
        deferred, self._deferred_events = self._deferred_events, []
        for event in deferred:
            self.render_event(event)

    def renderable_for(self, event: AgentEvent) -> RenderableType | None:
        """Map one agent event to a Rich renderable, if it should be displayed."""
        if isinstance(event, SessionStartedEvent):
            return Text(
                f"Session {event.session_id} using {event.provider} · {event.model}", style="dim"
            )
        if isinstance(event, TurnStartedEvent):
            return _debug_json("turn", {"input": event.user_input}) if self.debug else None
        if isinstance(event, ContextAssembledEvent):
            if not self.debug:
                return None
            return context_table(
                event.report,
                provider=None,
                model=event.model,
                title="Context assembled",
                prefix=f"iteration {event.iteration}; ",
            )
        if isinstance(event, ContextCompactionStartedEvent):
            if self.debug:
                return _debug_json(
                    "context compaction started",
                    {
                        "operation_id": event.operation_id,
                        "previous_checkpoint_id": event.previous_checkpoint_id,
                        "covered_message_count": event.covered_message_count,
                        "newly_covered_message_count": event.newly_covered_message_count,
                        "retained_message_count": event.retained_message_count,
                        "source_digest": event.source_digest,
                        "estimated_tokens_before": event.estimated_tokens_before,
                    },
                )
            return Text(
                f"Compacting {event.covered_message_count} historical messages…",
                style="dim",
            )
        if isinstance(event, ContextCompactionFinishedEvent):
            if self.debug:
                return _debug_json(
                    "context compaction finished",
                    {
                        "operation_id": event.operation_id,
                        "checkpoint_id": event.checkpoint_id,
                        "previous_checkpoint_id": event.previous_checkpoint_id,
                        "covered_message_count": event.covered_message_count,
                        "newly_covered_message_count": event.newly_covered_message_count,
                        "retained_message_count": event.retained_message_count,
                        "summary_chars": event.summary_chars,
                        "source_digest": event.source_digest,
                        "estimated_tokens_before": event.estimated_tokens_before,
                        "estimated_tokens_after": event.estimated_tokens_after,
                        "usage": event.usage.model_dump(mode="json"),
                        "before_sections": (
                            [
                                section.model_dump(mode="json")
                                for section in event.before_report.sections
                            ]
                            if event.before_report is not None
                            else None
                        ),
                        "after_sections": (
                            [
                                section.model_dump(mode="json")
                                for section in event.after_report.sections
                            ]
                            if event.after_report is not None
                            else None
                        ),
                    },
                )
            return Text(
                f"Compacted {event.covered_message_count} messages: "
                f"~{event.estimated_tokens_before:,} → "
                f"~{event.estimated_tokens_after:,} tokens",
                style="green",
            )
        if isinstance(event, ContextCompactionFailedEvent):
            if self.debug:
                return _debug_json(
                    "context compaction failed",
                    {
                        "operation_id": event.operation_id,
                        "previous_checkpoint_id": event.previous_checkpoint_id,
                        "error_type": event.error_type,
                        "message": event.message,
                        "provider_request_started": event.provider_request_started,
                        "usage": event.usage.model_dump(mode="json"),
                    },
                )
            return Text(f"Compaction failed: {event.message}", style="yellow")
        if isinstance(event, LlmRequestStartedEvent):
            if not self.debug:
                return None
            return Text(
                f"LLM request: {event.model} "
                f"({event.message_count} messages, {event.tool_count} tools)",
                style="dim",
            )
        if isinstance(event, LlmResponseFinishedEvent):
            if not self.debug:
                return None
            return _debug_json(
                "LLM response",
                {
                    "iteration": event.iteration,
                    "stop_reason": event.stop_reason,
                    "text_chars": event.text_chars,
                    "thinking_chars": event.thinking_chars,
                    "tool_call_count": event.tool_call_count,
                    "empty": event.empty,
                },
            )
        if isinstance(event, ThinkingDeltaEvent):
            return Text(f"[thinking] {event.delta}", style="dim") if self.debug else None
        if isinstance(event, ToolCallRequestedEvent):
            summary = summarize_tool_call(event.tool_name, event.args)
            if self.debug:
                return _debug_json(
                    f"tool requested: {event.tool_name}",
                    {"call_id": event.call_id, "args": event.args},
                )
            return Text(f"[tool] {event.tool_name}: {summary}", style="cyan")
        if isinstance(event, ToolCallNormalizedEvent):
            if not self.debug:
                return None
            return _debug_json(
                f"tool arguments normalized: {event.tool_name}",
                {"call_id": event.call_id, "paths": event.paths},
            )
        if isinstance(event, ToolCallRejectedEvent):
            return Text(
                f"[tool] {event.tool_name}: rejected ({event.reason})",
                style="red",
            )
        if isinstance(event, PermissionRequestedEvent):
            if not self.debug:
                return None
            return _debug_json(
                f"permission requested: {event.tool_name}",
                {"call_id": event.call_id, "reason": event.reason, "args": event.args},
            )
        if isinstance(event, PermissionDecidedEvent):
            style = "green" if event.decision == "allow" else "red"
            remembered = f" remembered: {event.grant_label}" if event.grant_label else ""
            return Text(
                f"[permission] {event.tool_name}: {event.decision}{remembered} ({event.reason})",
                style=style,
            )
        if isinstance(event, ToolCallStartedEvent):
            return Text(f"[tool] {event.tool_name}: running", style="cyan") if self.debug else None
        if isinstance(event, ToolCallFinishedEvent):
            status = "error" if event.is_error else "ok"
            style = "red" if event.is_error else "green"
            if self.debug:
                return _debug_json(
                    f"tool finished: {event.tool_name}",
                    {
                        "call_id": event.call_id,
                        "status": status,
                        "content_chars": event.content_chars,
                        "full_content_chars": event.full_content_chars,
                        "visible_content_chars": event.visible_content_chars,
                        "artifact_id": event.artifact_id,
                        "offloaded": event.offloaded,
                        "effect_kind": event.effect_kind,
                        "effect_disposition": event.effect_disposition,
                        "content": event.content,
                    },
                )
            return Text(
                f"[tool] {event.tool_name}: {status}, {event.content_chars} chars"
                + (f" (offloaded {event.full_content_chars} chars)" if event.offloaded else ""),
                style=style,
            )
        if isinstance(event, ToolResultOffloadFailedEvent):
            if self.debug:
                return _debug_json(
                    f"tool result offload failed: {event.tool_name}",
                    {
                        "call_id": event.call_id,
                        "full_content_chars": event.full_content_chars,
                        "visible_content_chars": event.visible_content_chars,
                        "error_type": event.error_type,
                    },
                )
            return Text(
                f"[tool] {event.tool_name}: full result could not be stored; "
                "the visible result is non-recoverably truncated",
                style="yellow",
            )
        if isinstance(event, TasksUpdatedEvent):
            return task_table(event.tasks)
        if isinstance(event, UserInteractionRequiredEvent):
            return Markdown(event.prompt)
        if isinstance(event, SkillActivatedEvent):
            replaced = f" (replaced {event.replaced_skill})" if event.replaced_skill else ""
            args = f" {event.args}" if event.args else ""
            if self.debug:
                return _debug_json(
                    "skill activated",
                    {
                        "session_id": event.session_id,
                        "turn_id": event.turn_id,
                        "skill_name": event.skill_name,
                        "args": event.args,
                        "source_path": event.source_path,
                        "replaced_skill": event.replaced_skill,
                    },
                )
            return Text(f"[skill] {event.skill_name}{args}{replaced}", style="magenta")
        if isinstance(event, WorkflowEvent):
            if event.action == "context_debug" and not self.debug:
                return None
            if event.action == "message_emitted":
                text = event.details.get("text")
                if isinstance(text, str):
                    return Panel(
                        Markdown(text),
                        title=f"workflow: {event.workflow_name}",
                        border_style="magenta",
                    )
            if event.action in {"scheduler_pass", "checkpoint_written"} and not self.debug:
                return None
            address = event.execution_address or event.step_id
            subject = f" {address}" if address else ""
            reason = f": {event.reason}" if event.reason else ""
            style = (
                "red"
                if event.action in {"step_attempt_failed", "step_in_doubt", "checkpoint_failed"}
                else "yellow"
                if event.action in {"step_skipped", "step_blocked", "step_interrupted"}
                else "green"
                if event.action in {"step_completed", "run_completed"}
                else "magenta"
            )
            return Text(f"[workflow v2] {event.action}{subject}{reason}", style=style)
        if isinstance(event, TurnFinishedEvent):
            if event.interrupted:
                return Text("Turn interrupted.", style="yellow")
            if event.error is not None:
                return Text(f"Turn failed: {event.error}", style="red")
            if self.debug:
                usage = event.usage
                return Text(
                    f"Turn finished after {event.iterations} iteration(s); "
                    f"usage prompt={usage.prompt_tokens} completion={usage.completion_tokens}",
                    style="dim",
                )
            return None
        if isinstance(event, AgentErrorEvent):
            return Text(f"{event.error_type}: {event.message}", style="red")
        return None

    def finish_stream(self) -> None:
        """Stop transient activity before an interactive or standalone render."""
        self._stop_activity()

    def _render_assistant_buffer(self, *, partial: bool) -> None:
        """Render one complete provider response, or preserve an interrupted partial."""
        text = "".join(self._assistant_buffer)
        self._assistant_buffer.clear()
        if not text.strip():
            return
        if partial:
            self.console.print(Text("Partial response (interrupted):", style="yellow"))
        self.console.print(Markdown(text.rstrip()))

    def _set_activity(self, message: str) -> None:
        """Display one replaceable activity indicator on interactive terminals."""
        self._activity_message = message
        if not self.console.is_terminal:
            return
        if self._activity is None:
            self._activity = self.console.status(message, spinner="dots")
            self._activity.start()
        else:
            self._activity.update(message)

    def _stop_activity(self) -> None:
        """Remove the current transient activity indicator."""
        self._activity_message = None
        if self._activity is not None:
            self._activity.stop()
            self._activity = None

    def _refresh_tool_activity(self) -> None:
        """Show the currently running tool or a compact parallel-tool count."""
        if not self._active_tools:
            self._stop_activity()
            return
        if len(self._active_tools) == 1:
            tool_name = next(iter(self._active_tools.values()))
            self._set_activity(f"Running {tool_name}…")
            return
        self._set_activity(f"Running {len(self._active_tools)} tools…")

    def render_config(
        self,
        settings: RickySettings,
        *,
        installation_config_path: Path,
        profile_config_path: Path,
        profile_secrets_path: Path,
    ) -> None:
        """Render resolved settings without exposing secret values."""
        table = Table(title="ricky configuration", show_header=True, header_style="bold")
        table.add_column("Setting")
        table.add_column("Value")

        openrouter_key = "set" if settings.openrouter_api_key is not None else "not set"
        anthropic_key = "set" if settings.anthropic_api_key is not None else "not set"
        brave_search_key = "set" if settings.brave_search_api_key is not None else "not set"
        table.add_row("default_provider", settings.default_provider)
        table.add_row(
            "providers.openrouter.default_model", settings.providers.openrouter.default_model
        )
        table.add_row(
            "providers.anthropic.default_model", settings.providers.anthropic.default_model
        )
        table.add_row(
            "providers.anthropic.default_max_tokens",
            str(settings.providers.anthropic.default_max_tokens),
        )
        table.add_row(
            "providers.claude_code.default_model",
            settings.providers.claude_code.default_model,
        )
        table.add_row("providers.claude_code.cli_path", settings.providers.claude_code.cli_path)
        table.add_row("openrouter_api_key", openrouter_key)
        table.add_row("anthropic_api_key", anthropic_key)
        table.add_row("request_timeout_seconds", str(settings.request_timeout_seconds))
        table.add_row("max_turn_iterations", str(settings.max_turn_iterations))
        table.add_row("context_char_limit", str(settings.context_char_limit))
        table.add_row("context.chars_per_token", str(settings.context.chars_per_token))
        table.add_row(
            "context.response_reserve_tokens", str(settings.context.response_reserve_tokens)
        )
        table.add_row("context.safety_margin_tokens", str(settings.context.safety_margin_tokens))
        table.add_row("context.models", str(len(settings.context.models)))
        table.add_row("browser.enabled", str(settings.browser.enabled))
        table.add_row("browser.browser_kind", settings.browser.browser_kind)
        table.add_row("browser.headless", str(settings.browser.headless))
        table.add_row("browser.binary_dir", settings.browser.binary_dir)
        table.add_row("browser.ephemeral_dir", settings.browser.ephemeral_dir)
        table.add_row("browser.persistent_dir", settings.browser.persistent_dir)
        table.add_row("browser.lease_dir", settings.browser.lease_dir)
        table.add_row(
            "browser.attachment_timeout_seconds",
            str(settings.browser.attachment_timeout_seconds),
        )
        tool_results = settings.context.tool_results
        table.add_row("context.tool_results.enabled", str(tool_results.enabled))
        table.add_row(
            "context.tool_results.offload_threshold_chars",
            str(tool_results.offload_threshold_chars),
        )
        table.add_row(
            "context.tool_results.inline_excerpt_chars",
            str(tool_results.inline_excerpt_chars),
        )
        table.add_row(
            "context.tool_results.head_fraction",
            str(tool_results.head_fraction),
        )
        table.add_row(
            "context.tool_results.artifact_max_chars",
            str(tool_results.artifact_max_chars),
        )
        table.add_row(
            "context.tool_results.session_artifact_max_chars",
            str(tool_results.session_artifact_max_chars),
        )
        table.add_row(
            "context.tool_results.read_chunk_chars",
            str(tool_results.read_chunk_chars),
        )
        compaction = settings.context.compaction
        table.add_row("context.compaction.enabled", str(compaction.enabled))
        table.add_row(
            "context.compaction.keep_recent_tokens",
            str(compaction.keep_recent_tokens),
        )
        table.add_row(
            "context.compaction.max_summary_tokens",
            str(compaction.max_summary_tokens),
        )
        table.add_row(
            "context.compaction.max_focus_chars",
            str(compaction.max_focus_chars),
        )
        table.add_row(
            "context.compaction.max_summary_chars",
            str(compaction.max_summary_chars),
        )
        table.add_row("shell_timeout_seconds", str(settings.shell_timeout_seconds))
        table.add_row("web_search.provider", settings.web_search.provider)
        table.add_row("web_search.country", settings.web_search.country)
        table.add_row("web_search.search_language", settings.web_search.search_language)
        table.add_row(
            "web_search.request_timeout_seconds",
            str(settings.web_search.request_timeout_seconds),
        )
        table.add_row("web_search.read_retry_limit", str(settings.web_search.read_retry_limit))
        table.add_row(
            "web_search.max_retry_delay_seconds",
            str(settings.web_search.max_retry_delay_seconds),
        )
        table.add_row("web_search.download_dir", settings.web_search.download_dir)
        table.add_row(
            "web_search.brave.api_base_url",
            settings.web_search.providers.brave.api_base_url,
        )
        for effort in ("quick", "standard", "deep"):
            budget = getattr(settings.web_search.efforts, effort)
            limits = (
                f"candidates={budget.candidate_count}, sources={budget.source_limit}, "
                f"context_tokens={budget.context_token_limit}, snippets={budget.snippet_limit}, "
                f"tokens_per_source={budget.tokens_per_source}, "
                f"snippets_per_source={budget.snippets_per_source}, "
                f"result_chars={budget.result_char_limit}, relevance={budget.relevance_mode}"
            )
            table.add_row(f"web_search.efforts.{effort}", limits)
        table.add_row("brave_search_api_key", brave_search_key)
        table.add_row("google.token_store_path", settings.google.token_store_path)
        table.add_row(
            "google.auth_callback_timeout_seconds",
            str(settings.google.auth_callback_timeout_seconds),
        )
        for account, identity in sorted(settings.google.accounts.items()):
            table.add_row(f"google.accounts.{account}.email", identity.email)
            state = "set" if account in settings.google_oauth_clients else "not set"
            table.add_row(f"google_oauth_clients.{account}", state)
        table.add_row("gmail.api_base_url", settings.gmail.api_base_url)
        table.add_row("gmail.download_dir", settings.gmail.download_dir)
        table.add_row("gmail.default_list_limit", str(settings.gmail.default_list_limit))
        table.add_row("gmail.body_char_limit", str(settings.gmail.body_char_limit))
        table.add_row("gcal.api_base_url", settings.gcal.api_base_url)
        table.add_row("gcal.default_list_limit", str(settings.gcal.default_list_limit))
        table.add_row(
            "gcal.default_window_days",
            str(settings.gcal.default_window_days),
        )
        table.add_row("gcal.description_char_limit", str(settings.gcal.description_char_limit))
        table.add_row("user_data_dir", settings.user_data_dir)
        table.add_row("user_data_path", str(user_data_path(settings)))
        table.add_row("memory.enabled", str(settings.memory.enabled))
        table.add_row("profiles.default", settings.profiles.default)
        table.add_row("profiles.enabled", ", ".join(settings.profiles.enabled))
        table.add_row("memory.index_char_limit", str(settings.memory.index_char_limit))
        table.add_row("memory.recall_char_limit", str(settings.memory.recall_char_limit))
        table.add_row("memory.recall_note_limit", str(settings.memory.recall_note_limit))
        table.add_row("memory.note_body_char_limit", str(settings.memory.note_body_char_limit))

        for label, path in (
            ("installation config", installation_config_path),
            ("profile config", profile_config_path),
            ("profile secrets", profile_secrets_path),
        ):
            state = "exists" if path.exists() else "absent"
            table.add_row(label, f"{path} ({state})")

        self.console.print(table)

    def render_help(self) -> None:
        """Render slash command help."""
        self.console.print(
            Panel(
                Markdown(
                    """
`/help` show commands
`/debug` toggle verbose event rendering
`/tasks` show the current task list
`/context` inspect stored context without a model call
`/compact [focus]` summarize old context and retain the recent verbatim tail
`/model` show the session provider and model
`/clear` start a fresh session
`/permissions` list active session permission grants
`/permissions clear` revoke all session permission grants
`/remember <text>` remember a durable fact
`/remember` propose memory notes from the conversation
`/skill` list loaded skills
`/skill <name> [args]` activate a prompt skill
`/workflow` list loaded workflows
`/workflow <name> [k=v ...]` run a workflow
`/quit` exit

**Editing:** `Enter` sends; `Ctrl+J` inserts a newline; `Tab` completes commands and skills;
`Ctrl+R` searches this chat's input history; `Ctrl+X Ctrl+E` edits the draft externally.
""".strip()
                ),
                title="Commands",
            )
        )

    def render_welcome(self, session: AgentSession) -> None:
        """Render the REPL banner."""
        profiles = ", ".join(session.profile_scope.profiles)
        self.console.print(
            Text(
                f"ricky chat ({session.provider} · {session.model}) · "
                f"profile: {session.profile_scope.primary} · access: {profiles}",
                style="bold",
            )
        )
        self.console.print(Text("Type /help for commands, /quit to exit.", style="dim"))

    def render_skills(self, skill_registry: SkillRegistry) -> None:
        """Render loaded skills and load errors."""
        self.finish_stream()
        skills = skill_registry.skills()
        if skills:
            table = Table(title="Skills", show_header=True)
            table.add_column("Name")
            table.add_column("Description")
            for skill in skills:
                table.add_row(skill.name, skill.description)
            self.console.print(table)
        else:
            self.console.print(Text("No skills loaded.", style="dim"))
        self.render_skill_load_errors(skill_registry.errors)

    def render_skill_load_errors(self, errors: list[SkillLoadError]) -> None:
        """Render malformed skill files discovered at startup."""
        if not errors:
            return
        self.finish_stream()
        table = Table(title="Skill load errors", show_header=True)
        table.add_column("File")
        table.add_column("Error")
        for error in errors:
            table.add_row(error.source_path, error.message)
        self.console.print(table)

    def render_workflow_list(self, workflow_registry: WorkflowRegistry) -> None:
        """Render loaded workflows and load errors."""
        self.finish_stream()
        workflows = workflow_registry.workflows()
        if workflows:
            table = Table(title="Workflows", show_header=True)
            table.add_column("Name")
            table.add_column("Steps", justify="right")
            table.add_column("Description")
            for spec in workflows:
                table.add_row(spec.name, str(len(spec.steps)), spec.description)
            self.console.print(table)
        else:
            self.console.print(Text("No workflows loaded.", style="dim"))
        self.render_workflow_load_errors(workflow_registry.errors)

    def render_workflow_load_errors(self, errors: list[WorkflowLoadError]) -> None:
        """Render unavailable or invalid workflow bundles discovered at startup."""
        if not errors:
            return
        self.finish_stream()
        table = Table(title="Workflow discovery issues", show_header=True)
        table.add_column("Source", overflow="fold", max_width=40)
        table.add_column("Details")
        for error in errors:
            table.add_row(Text(error.source_path), Text(error.message))
        self.console.print(table)

    def render_workflow_show(self, description: str) -> None:
        """Render the plain-text graph description of one workflow."""
        self.finish_stream()
        self.console.print(Panel(Text(description), title="Workflow", border_style="magenta"))

    def render_workflow_dryrun(self, name: str, status: str, reason: str | None) -> None:
        """Render the dry-run outcome summary."""
        self.finish_stream()
        style = "green" if status == "completed" else "red"
        detail = f" ({reason})" if reason else ""
        self.console.print(
            Text(
                f"Dry run of '{name}' {status}{detail}. No mutating tool ran; "
                "confirm gates and shell checks were logged, not executed.",
                style=style,
            )
        )

    def render_memory_load_errors(self, errors: list[MemoryLoadError]) -> None:
        """Render malformed memory notes discovered at startup."""
        if not errors:
            return
        self.finish_stream()
        table = Table(title="Memory load errors", show_header=True)
        table.add_column("File")
        table.add_column("Error")
        for error in errors:
            table.add_row(error.source_path, error.message)
        self.console.print(table)

    def render_memory_config(
        self,
        *,
        roots: dict[str, Path],
        counts: dict[str, int],
    ) -> None:
        """Render profile-owned memory roots and read-only note counts."""
        table = Table(title="Memory")
        table.add_column("Setting")
        table.add_column("Value")
        for profile, root in roots.items():
            table.add_row(f"{profile} root", str(root))
            table.add_row(f"{profile} notes", str(counts[profile]))
        self.console.print(table)

    def render_context(self, session: AgentSession, report: ContextReport) -> None:
        """Render an on-demand prospective context report."""
        self.finish_stream()
        self.console.print(
            context_table(report, provider=session.provider, model=session.model, title="Context")
        )

    def render_status(self, message: str, *, style: str = "dim") -> None:
        """Render a short status line."""
        self.finish_stream()
        self.console.print(Text(message, style=style))

    def render_error(self, message: str) -> None:
        """Render a CLI error."""
        self.finish_stream()
        self.console.print(Text(message, style="red"))

    def render_tasks(self, session: AgentSession) -> None:
        """Render the current session task list."""
        self.finish_stream()
        self.console.print(task_table(session.task_snapshots()))

    def render_permissions(self, session: AgentSession) -> None:
        """Render active session permission grants."""
        self.finish_stream()
        grants = session.permission_grants
        if not grants:
            self.console.print(Text("No active permission grants.", style="dim"))
            return
        table = Table(title="Session permission grants", show_header=True)
        table.add_column("Tool")
        table.add_column("Scope")
        for grant in grants:
            table.add_row(grant.tool_name, _grant_scope_text(grant))
        self.console.print(table)


def context_table(
    report: ContextReport,
    *,
    provider: str | None,
    model: str,
    title: str,
    prefix: str = "",
) -> Table:
    """Build the shared debug and on-demand context table."""
    table = Table(title=title, show_header=True)
    table.add_column("Section")
    table.add_column("Chars", justify="right")
    table.add_column("Est. tokens", justify="right")
    table.add_column("Count", justify="right")
    for section in report.sections:
        table.add_row(
            section.name,
            str(section.chars),
            str(section.estimated_tokens),
            str(section.item_count),
        )
    if report.checkpoint is not None:
        checkpoint = report.checkpoint
        table.add_row(
            f"active checkpoint {checkpoint.id} ({checkpoint.created_at})",
            str(checkpoint.summary_chars),
            (
                f"{checkpoint.estimated_tokens_before} → "
                f"{checkpoint.estimated_tokens_after} "
                f"(Δ {checkpoint.estimated_token_reduction})"
            ),
            str(checkpoint.covered_raw_messages),
            style="magenta",
        )
        table.add_row(
            "retained raw history",
            "—",
            "—",
            str(checkpoint.retained_raw_messages),
            style="dim",
        )
        table.add_row(
            "available original history / artifact references",
            "—",
            "—",
            (f"{checkpoint.original_history_messages} / {checkpoint.artifact_reference_count}"),
            style="dim",
        )
    if report.artifact_count:
        table.add_row(
            "tool_result_artifacts (stored; excluded from input)",
            str(report.stored_artifact_chars),
            "—",
            str(report.artifact_count),
            style="dim",
        )
    table.add_section()
    table.add_row(
        "Total",
        str(report.serialized_chars),
        str(report.estimated_input_tokens),
        "—",
        style="bold",
    )

    budget = report.budget
    selection = f"{provider} · {model}" if provider is not None else model
    if budget.hard_input_tokens is None:
        hard = utilization = remaining = "unknown"
    else:
        hard = str(budget.hard_input_tokens)
        utilization = (
            f"{report.estimated_input_tokens / budget.hard_input_tokens:.1%}"
            if budget.hard_input_tokens
            else "unknown"
        )
        remaining = str(budget.remaining_tokens)
    note = (
        "excludes your next user message"
        if not report.pending_user_input_included
        else "includes pending user input"
    )
    table.caption = (
        f"{prefix}{selection}; input {report.estimated_input_tokens} tokens / "
        f"{report.serialized_chars} chars\n"
        f"hard input {hard}; utilization {utilization}; headroom {remaining}\n"
        f"response reserve {budget.output_reserve_tokens}; "
        f"safety margin {budget.safety_margin_tokens}\n"
        f"messages {report.message_count}; advertised tools {report.tool_count}\n"
        f"artifacts {report.artifact_count} / {report.stored_artifact_chars} stored chars\n"
        f"capacity {budget.capacity_source}; {note}"
    )
    return table


def task_table(tasks: list[dict[str, str]]) -> RenderableType:
    """Build a task-list renderable."""
    if not tasks:
        return Text("No tasks.", style="dim")
    table = Table(title="Tasks", show_header=True)
    table.add_column("Status")
    table.add_column("Task")
    for task in tasks:
        table.add_row(_status_marker(task.get("status", "pending")), task.get("title", ""))
    return table


def summarize_tool_call(tool_name: str, args: dict[str, Any]) -> str:
    """Return a compact human-readable summary for a tool invocation."""
    if tool_name == "run_shell":
        return _short(str(args.get("command", "")))
    if tool_name in {"read_file", "write_file", "edit_file"}:
        return _short(str(args.get("path", "")))
    if tool_name == "list_dir":
        return _short(str(args.get("path", ".")))
    if tool_name == "glob_search":
        return _short(str(args.get("pattern", "")))
    if tool_name == "grep_search":
        regex = str(args.get("regex", ""))
        path = str(args.get("path", "."))
        return _short(f"{regex} in {path}")
    if tool_name == "update_tasks":
        tasks = args.get("tasks")
        count = len(tasks) if isinstance(tasks, list) else 0
        return f"{count} task(s)"
    if tool_name == "use_skill":
        name = str(args.get("name", ""))
        skill_args = str(args.get("args", ""))
        return _short(f"{name} {skill_args}".strip())
    return _short(json.dumps(args, sort_keys=True, default=str))


def _debug_json(title: str, data: dict[str, Any]) -> RenderableType:
    return Panel(
        JSON(json.dumps(data, indent=2, sort_keys=True, default=str)),
        title=title,
        border_style="dim",
    )


def _grant_scope_text(grant: PermissionGrant) -> str:
    """Describe a grant's breadth for the /permissions table."""
    if grant.label:
        return grant.label
    if grant.params_equal:
        return ", ".join(f"{key}={value}" for key, value in sorted(grant.params_equal.items()))
    return "any params"


def _short(value: str, *, limit: int = 140) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def _status_marker(status: str) -> str:
    if status == "done":
        return "x"
    if status == "in_progress":
        return ">"
    return " "
