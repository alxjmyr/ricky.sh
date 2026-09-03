"""Async chat REPL controller."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ricky.agent.events import SkillActivatedEvent, TurnFinishedEvent
from ricky.agent.loop import AgentLoop
from ricky.agent.session import AgentSession
from ricky.agent.workflow import WorkflowService
from ricky.config import RickySettings
from ricky.durable_tasks.store import TaskStoreError
from ricky.interfaces.cli.render import CliRenderer
from ricky.skills.registry import SkillRegistry
from ricky.workflows.registry import WorkflowRegistry

if TYPE_CHECKING:
    from ricky.agent.artifacts import SessionArtifactStore
    from ricky.durable_tasks.scoped import ScopedDurableTaskStore
    from ricky.media import SessionMediaStore
    from ricky.memory.store import MemoryStore

MEMORY_REFLECTION_PROMPT = (
    "Review the conversation for durable, material, non-obvious facts\n"
    "that would help in future sessions. Propose a numbered list of memory notes. "
    "When proposing memory notes assume\n"
    "that a future model or reader has ADHD. All memories should be clear, direct, facts, "
    "or conclusions with a few words as possible\n"
    "to convery the information.\n"
    "For each proposal, show the profile, slug, title, type, summary, tags, related\n"
    "slugs, source, and complete body. Do not call remember in this turn. Wait for\n"
    "the user's approval or edits. In a later turn, call remember only for the\n"
    "approved notes."
)


@dataclass
class ChatController:
    """Drive one interactive agent session."""

    agent_loop: AgentLoop
    session: AgentSession
    settings: RickySettings
    renderer: CliRenderer
    skill_registry: SkillRegistry
    memory: MemoryStore | None = None
    durable_tasks: ScopedDurableTaskStore | None = None
    workflow_runner: WorkflowService | None = None
    workflow_registry: WorkflowRegistry | None = None
    session_artifacts: SessionArtifactStore | None = None
    session_media: SessionMediaStore | None = None

    async def run(self) -> None:
        """Run the REPL until the user exits."""
        self.renderer.configure_chat_input(self.skill_registry)
        self.renderer.render_welcome(self.session)
        self.renderer.render_skill_load_errors(self.skill_registry.errors)
        if self.workflow_registry is not None:
            self.renderer.render_workflow_load_errors(self.workflow_registry.errors)
        if self.memory is not None:
            self.renderer.render_memory_load_errors(self.memory.errors())
        idle_interrupt_seen = False
        while True:
            try:
                user_input = await self.renderer.read_user_input()
            except EOFError:
                await self._release_task_leases()
                self.renderer.render_status("Exiting.")
                return
            except (KeyboardInterrupt, asyncio.CancelledError):
                if idle_interrupt_seen:
                    await self._release_task_leases()
                    self.renderer.render_status("Exiting.")
                    return
                idle_interrupt_seen = True
                self.renderer.render_status("Press Ctrl+C again to exit.", style="yellow")
                continue

            user_input = user_input.strip()
            if not user_input:
                continue
            idle_interrupt_seen = False

            if user_input.startswith("/"):
                should_continue = await self._handle_slash_command(user_input)
                if not should_continue:
                    await self._release_task_leases()
                    return
                continue

            await self._run_turn(user_input)

    async def _run_turn(self, user_input: str) -> None:
        task = asyncio.create_task(self._consume_turn(user_input))
        try:
            await task
        except (KeyboardInterrupt, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._discard_queued_workflow()
            self.renderer.render_status("Turn cancelled.", style="yellow")

    async def _consume_turn(self, user_input: str) -> None:
        finished: TurnFinishedEvent | None = None
        async for event in self.agent_loop.run_turn(self.session, user_input):
            self.renderer.render_event(event)
            if isinstance(event, TurnFinishedEvent):
                finished = event
        if finished is None or finished.interrupted or finished.error is not None:
            self._discard_queued_workflow()
            return
        # start_workflow only queues run state; drain it once the turn ends.
        # The started flag stops a re-entry when a workflow is already running.
        queued = self.session.active_workflow
        if self.workflow_runner is not None and queued is not None and not queued.started:
            async for event in self.workflow_runner.start(self.session, queued.name, queued.args):
                self.renderer.render_event(event)

    def _discard_queued_workflow(self) -> None:
        run_state = self.session.active_workflow
        if run_state is not None and not run_state.started:
            self.session.active_workflow = None

    async def _handle_slash_command(self, line: str) -> bool:
        command, _, arg = line.partition(" ")
        command = command.lower()
        arg = arg.strip()

        if command in {"/quit", "/exit", "/q"}:
            self.renderer.render_status("Exiting.")
            return False
        if command == "/help":
            self.renderer.render_help()
            return True
        if command == "/debug":
            if arg.lower() in {"on", "true", "1"}:
                self.renderer.debug = True
            elif arg.lower() in {"off", "false", "0"}:
                self.renderer.debug = False
            else:
                self.renderer.debug = not self.renderer.debug
            state = "on" if self.renderer.debug else "off"
            self.renderer.render_status(f"Debug mode {state}.")
            return True
        if command == "/tasks":
            self.renderer.render_tasks(self.session)
            return True
        if command == "/compact":
            await self._run_compaction(arg or None)
            return True
        if command == "/context":
            report = self.agent_loop.inspect_context(self.session)
            self.renderer.render_context(self.session, report)
            return True
        if command == "/model":
            self.renderer.render_status(
                f"Current model: {self.session.provider} · {self.session.model}"
            )
            if arg:
                self.renderer.render_status("Model selection is pinned for this session.")
            return True
        if command == "/clear":
            operation = asyncio.create_task(self._clear_session())
            interrupted = False
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    interrupted = True
                except Exception:
                    break
            operation.result()
            if interrupted:
                raise asyncio.CancelledError
            return True
        if command == "/remember":
            if self.memory is None:
                self.renderer.render_status("Memory is disabled for this session.", style="yellow")
                return True
            prompt = f"Remember this: {arg}" if arg else MEMORY_REFLECTION_PROMPT
            await self._run_turn(prompt)
            return True
        if command == "/permissions":
            if arg.lower() in {"clear", "reset"}:
                count = len(self.session.permission_grants)
                self.session.permission_grants.clear()
                self.renderer.render_status(f"Cleared {count} session permission grant(s).")
            else:
                self.renderer.render_permissions(self.session)
            return True
        if command == "/workflow":
            if self.workflow_runner is None or self.workflow_registry is None:
                self.renderer.render_status(
                    "Workflows are disabled for this session.", style="yellow"
                )
                return True
            if not arg:
                self.renderer.render_workflow_list(self.workflow_registry)
                return True
            workflow_name, _, workflow_args = arg.partition(" ")
            try:
                parsed_args = _typed_workflow_args(parse_workflow_args(workflow_args))
            except ValueError as exc:
                self.renderer.render_status(str(exc), style="yellow")
                return True
            if self.session.active_workflow is not None:
                active_name = self.session.active_workflow.name
                self.renderer.render_status(
                    f"Workflow '{active_name}' is already "
                    "active or queued; one workflow runs at a time.",
                    style="yellow",
                )
                return True
            await self._run_workflow(workflow_name, parsed_args)
            return True
        if command == "/skill":
            if not arg:
                self.renderer.render_skills(self.skill_registry)
                return True
            skill_name, _, skill_args = arg.partition(" ")
            activation = self.skill_registry.activate(self.session, skill_name, skill_args)
            if not activation.ok:
                self.renderer.render_status(
                    activation.error or "Skill activation failed.", style="yellow"
                )
                return True
            assert activation.skill is not None
            self.renderer.render_event(
                SkillActivatedEvent(
                    session_id=self.session.id,
                    skill_name=activation.skill.name,
                    args=activation.skill.args,
                    source_path=activation.skill.source_path,
                    replaced_skill=activation.previous_skill,
                )
            )
            return True

        self.renderer.render_status(f"Unknown command: {command}", style="yellow")
        return True

    async def _clear_session(self) -> None:
        """Complete one resident session generation transition."""

        current = self.session
        fresh = AgentSession.create(
            self.settings,
            profile_scope=current.profile_scope,
            provider=current.provider,
            model=current.model,
        )
        await self._release_task_leases()
        if self.session_artifacts is not None:
            await self.session_artifacts.reset_for_session(fresh.id)
        if self.session_media is None:
            self.session = fresh
        else:
            await self.session_media.reset_for_session(current, fresh.id)
            for field_name in AgentSession.model_fields:
                setattr(current, field_name, getattr(fresh, field_name))
            self.session = current
        self.renderer.render_status("Started a fresh session.")

    async def _release_task_leases(self) -> None:
        if self.durable_tasks is None:
            return
        try:
            await self.durable_tasks.release_session_leases(self.session.id)
        except TaskStoreError:
            # Expiry remains the correctness path when graceful cleanup fails.
            pass
        finally:
            self.session.active_task_leases.clear()

    async def _run_workflow(self, name: str, args: dict[str, Any]) -> None:
        task = asyncio.create_task(self._consume_workflow(name, args))
        try:
            await task
        except (KeyboardInterrupt, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.renderer.render_status("Workflow cancelled.", style="yellow")

    async def _run_compaction(self, focus: str | None) -> None:
        task = asyncio.create_task(self._consume_compaction(focus))
        try:
            await task
        except (KeyboardInterrupt, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.renderer.render_status("Compaction cancelled.", style="yellow")

    async def _consume_compaction(self, focus: str | None) -> None:
        async for event in self.agent_loop.compact_context(self.session, focus):
            self.renderer.render_event(event)

    async def _consume_workflow(self, name: str, args: dict[str, Any]) -> None:
        runner = self.workflow_runner
        if runner is None:
            raise ValueError(f"no runner is available for workflow {name!r}")
        async for event in runner.start(self.session, name, args):
            self.renderer.render_event(event)


def parse_workflow_args(text: str) -> dict[str, Any]:
    """Parse space-separated key=value invocation arguments."""
    args: dict[str, Any] = {}
    for token in text.split():
        key, separator, value = token.partition("=")
        if not separator or not key:
            raise ValueError(f"Workflow args must be key=value pairs; got: {token}")
        args[key] = value
    return args


def _typed_workflow_args(args: dict[str, Any]) -> dict[str, Any]:
    """Decode JSON typed values while preserving ordinary unquoted strings."""

    result: dict[str, Any] = {}
    for name, raw in args.items():
        if not isinstance(raw, str):
            result[name] = raw
            continue
        try:
            result[name] = json.loads(raw)
        except json.JSONDecodeError:
            result[name] = raw
    return result
