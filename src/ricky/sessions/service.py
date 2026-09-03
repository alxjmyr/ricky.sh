"""One-runtime-per-turn coordination for persistent conversations."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from ricky.agent.events import (
    AgentEvent,
    LlmResponseFinishedEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    TurnFinishedEvent,
    UserInteractionRequiredEvent,
)
from ricky.config import RickySettings
from ricky.owned_operation import run_with_lease_heartbeat
from ricky.profiles import ProfileScope
from ricky.sessions.store import SessionStore
from ricky.sessions.types import StoredSession, StoredTurn

if TYPE_CHECKING:
    from ricky.runtime import SessionRuntime

EventSink = Callable[[AgentEvent], Awaitable[None] | None]
RuntimeBuilder = Callable[..., AbstractAsyncContextManager["SessionRuntime"]]


class PersistentTurnError(RuntimeError):
    """A bounded persistent turn did not commit successfully."""


class WorkflowResumeUnsupportedError(PersistentTurnError):
    """Stored workflow state cannot yet be resumed through this coordinator."""


class Clock(Protocol):
    def __call__(self) -> datetime: ...


class PersistentTurnService:
    """Lease, run, close, and atomically persist one bounded agent turn."""

    def __init__(
        self,
        settings: RickySettings,
        store: SessionStore,
        *,
        profile_scope: ProfileScope,
        runtime_builder: RuntimeBuilder | None = None,
        clock: Clock | None = None,
        runtime_kwargs: Mapping[str, Any] | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.profile_scope = profile_scope
        self._runtime_builder = runtime_builder
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runtime_kwargs = dict(runtime_kwargs or {})
        self._project_root = project_root

    async def run_turn(
        self,
        session_id: str,
        user_input: str,
        *,
        owner: str,
        inbound_ref: str | None = None,
        event_sink: EventSink | None = None,
        extra_system_sections: Mapping[str, str] | None = None,
    ) -> StoredSession:
        """Run exactly one turn and return its committed session snapshot."""

        lease = await self.store.acquire(
            session_id,
            owner,
            scope=self.profile_scope,
            lease_seconds=self.settings.sessions.lease_seconds,
        )
        turn: StoredTurn | None = None
        turn_started = False
        observable = False
        try:
            stored = await self.store.get(session_id, scope=self.profile_scope)
            if stored.session.active_workflow is not None:
                raise WorkflowResumeUnsupportedError(
                    "persistent workflow resume is not supported; "
                    "inspect and clear it interactively"
                )

            session = stored.session.model_copy(deep=True)
            session.permission_grants.clear()
            turn = StoredTurn(
                id=f"turn_{uuid4().hex}",
                session_id=session.id,
                profile_label=stored.profile_label,
                inbound_ref=inbound_ref,
                base_revision=stored.revision,
                status="running",
                started_at=self._utc_now(),
            )
            # Set this before awaiting: SQLite work is joined on cancellation and
            # therefore may have durably inserted the turn before cancellation lands.
            turn_started = True
            await self.store.begin_turn(lease, turn)
            runtime_builder = self._runtime_builder
            if runtime_builder is None:
                from ricky.runtime import build_session_runtime

                runtime_builder = build_session_runtime

            async def run_owned_turn() -> StoredSession:
                nonlocal observable
                final: TurnFinishedEvent | None = None
                async with asyncio.timeout(self.settings.sessions.turn_wall_seconds):
                    async with runtime_builder(
                        self.settings,
                        session=session,
                        project_root=self._project_root,
                        **self._runtime_kwargs,
                    ) as runtime:
                        # Durable task rows, not the serialized lease objects, are
                        # authoritative. Release them before exposing the session to the loop.
                        await runtime.durable_tasks.release_session_leases(session.id)
                        session.active_task_leases.clear()
                        if extra_system_sections is None:
                            events = runtime.agent_loop.run_turn(session, user_input)
                        else:
                            events = runtime.agent_loop.run_turn(
                                session,
                                user_input,
                                extra_system_sections=extra_system_sections,
                            )
                        async for event in events:
                            if _is_observable(event):
                                observable = True
                            if event_sink is not None:
                                result = event_sink(event)
                                if inspect.isawaitable(result):
                                    await result
                            if isinstance(event, TurnFinishedEvent):
                                final = event

                if final is None:
                    raise PersistentTurnError("agent turn ended without a turn_finished event")
                if final.interrupted:
                    raise PersistentTurnError("agent turn was interrupted")
                if final.error is not None:
                    raise PersistentTurnError(final.error)
                return await self.store.commit(lease, stored.revision, session, turn)

            try:
                return await run_with_lease_heartbeat(
                    run_owned_turn(),
                    lease=lease,
                    renew=self.store.renew,
                    interval_seconds=max(0.1, self.settings.sessions.lease_seconds / 3),
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if "lease" in str(exc).lower() or "fence" in str(exc).lower():
                    raise PersistentTurnError(f"session lease renewal failed: {exc}") from exc
                raise
        except BaseException as exc:
            if turn_started and turn is not None:
                await self._finalize_failed_turn(lease, turn, exc, observable)
            raise
        finally:
            with suppress(Exception):
                await self.store.release(lease)

    async def _finalize_failed_turn(
        self,
        lease: Any,
        turn: StoredTurn,
        exc: BaseException,
        observable: bool,
    ) -> None:
        """Resolve commit cancellation before marking an unfinished turn."""

        try:
            current = await self.store.get(turn.session_id, scope=self.profile_scope)
            if current.last_turn_id == turn.id and current.revision == turn.base_revision + 1:
                return
            recent = await self.store.turns(
                turn.session_id,
                scope=self.profile_scope,
                limit=min(50, self.settings.sessions.turn_retention),
            )
            matching = next((item for item in recent if item.id == turn.id), None)
            if matching is None or matching.status != "running":
                return
            message = str(exc).strip() or type(exc).__name__
            await self.store.fail_turn(lease, turn.id, message, uncertain=observable)
        except BaseException:
            # The original failure remains authoritative. A stale lease must not
            # mutate state; startup inspection can surface a leftover running turn.
            return

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("turn-service clock must return an aware datetime")
        return value.astimezone(UTC)


def _is_observable(event: AgentEvent) -> bool:
    """Whether replay could duplicate provider output or tool execution."""

    return isinstance(
        event,
        (
            TextDeltaEvent,
            ThinkingDeltaEvent,
            LlmResponseFinishedEvent,
            ToolCallStartedEvent,
            ToolCallFinishedEvent,
            UserInteractionRequiredEvent,
        ),
    )
