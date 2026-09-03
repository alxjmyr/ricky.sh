"""Deterministic context assembly and accounting for agent turns."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ricky.agent.context_types import (
    CheckpointContextReport,
    ContextBudget,
    ContextReport,
    ContextSection,
)
from ricky.agent.events import ContextAssembledEvent
from ricky.agent.prompts import SYSTEM_PROMPT_V1
from ricky.agent.session import AgentSession
from ricky.config import ContextSettings
from ricky.llm import (
    CompletionRequest,
    ImagePart,
    Message,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    UserContent,
)
from ricky.profiles import SHARED_PROFILE
from ricky.skills.registry import SkillRegistry
from ricky.skills.spec import ActiveSkill
from ricky.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from ricky.memory.store import MemoryStore
    from ricky.workflows.registry import WorkflowRegistry

DEFAULT_CONTEXT_CHAR_LIMIT = 120_000
SOUL_FILENAME = "SOUL.md"

_BASE_SECTIONS = (
    "base_system_prompt",
    "checkpoint_summary",
    "conversation_text",
    "conversation_images",
    "assistant_thinking",
    "tool_calls",
    "tool_results",
    "memory_index",
    "active_skill",
    "pending_user_input",
    "pending_user_images",
    "advertised_tool_definitions",
)


@dataclass(frozen=True)
class ContextAssembly:
    """The provider request plus one authoritative composition report and event."""

    request: CompletionRequest
    report: ContextReport
    event: ContextAssembledEvent


@dataclass(frozen=True)
class _CollectedContext:
    messages: list[Message]
    tools: list[ToolSpec]
    contributions: list[tuple[str, int, int]]
    pending_user_input_included: bool


def assemble_context(
    session: AgentSession,
    registry: ToolRegistry,
    *,
    turn_id: str,
    iteration: int,
    user_input: str | UserContent | None = None,
    cwd: Path | None = None,
    skill_registry: SkillRegistry | None = None,
    memory: MemoryStore | None = None,
    workflow_registry: WorkflowRegistry | None = None,
    extra_system_sections: Mapping[str, str] | None = None,
    max_completion_tokens: int | None = None,
    now: datetime | None = None,
) -> ContextAssembly:
    """Build and account for the exact canonical request sent by the loop."""
    request, report = _build_context(
        session,
        registry,
        user_input=user_input,
        cwd=cwd,
        skill_registry=skill_registry,
        memory=memory,
        workflow_registry=workflow_registry,
        extra_system_sections=extra_system_sections,
        max_completion_tokens=max_completion_tokens,
        now=now,
    )
    event = ContextAssembledEvent(
        turn_id=turn_id,
        iteration=iteration,
        model=session.model,
        report=report,
    )
    return ContextAssembly(request=request, report=report, event=event)


def inspect_context(
    session: AgentSession,
    registry: ToolRegistry,
    *,
    cwd: Path | None = None,
    skill_registry: SkillRegistry | None = None,
    memory: MemoryStore | None = None,
    workflow_registry: WorkflowRegistry | None = None,
    extra_system_sections: Mapping[str, str] | None = None,
    enforce_char_limit: bool = True,
    now: datetime | None = None,
) -> ContextReport:
    """Prospectively report stored context without input, events, or mutation."""
    _, report = _build_context(
        session,
        registry,
        user_input=None,
        cwd=cwd,
        skill_registry=skill_registry,
        memory=memory,
        workflow_registry=workflow_registry,
        extra_system_sections=extra_system_sections,
        max_completion_tokens=None,
        enforce_char_limit=enforce_char_limit,
        now=now,
    )
    return report


def source_history_digest(messages: list[Message]) -> str:
    """Return a stable digest for an exact canonical history prefix."""
    payload = json.dumps(
        [message.model_dump(mode="json") for message in messages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def estimate_context_tokens(session: AgentSession, chars: int) -> int:
    """Apply the session-snapshotted context estimator to a character count."""
    settings = ContextSettings.model_validate(session.settings_snapshot.get("context", {}))
    return _estimate_tokens(chars, settings.chars_per_token)


def account_completion_request(
    session: AgentSession,
    request: CompletionRequest,
    contributions: list[tuple[str, int, int]],
    *,
    pending_user_input_included: bool = False,
) -> ContextReport:
    """Account for an already-built canonical request without sending it."""
    return _account_request(
        session,
        request,
        contributions,
        pending_user_input_included,
    )


def serialize_canonical_request(request: CompletionRequest) -> str:
    """Serialize a canonical request deterministically for accounting."""
    return json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _build_context(
    session: AgentSession,
    registry: ToolRegistry,
    *,
    user_input: str | UserContent | None,
    cwd: Path | None,
    skill_registry: SkillRegistry | None,
    memory: MemoryStore | None,
    workflow_registry: WorkflowRegistry | None,
    extra_system_sections: Mapping[str, str] | None,
    max_completion_tokens: int | None,
    now: datetime | None,
    enforce_char_limit: bool = True,
) -> tuple[CompletionRequest, ContextReport]:
    collected = _collect_context(
        session,
        registry,
        user_input=user_input,
        cwd=cwd,
        skill_registry=skill_registry,
        memory=memory,
        workflow_registry=workflow_registry,
        extra_system_sections=extra_system_sections,
        now=now,
    )
    projected = _identity_projection(collected)
    request = CompletionRequest(
        model=session.model,
        messages=projected.messages,
        session_id=session.id,
        tools=projected.tools,
        max_tokens=max_completion_tokens,
    )
    report = _account_request(
        session,
        request,
        projected.contributions,
        projected.pending_user_input_included,
    )
    limit = int(session.settings_snapshot.get("context_char_limit", DEFAULT_CONTEXT_CHAR_LIMIT))
    if enforce_char_limit and report.serialized_chars > limit:
        raise ValueError(f"context character limit exceeded: {report.serialized_chars} > {limit}")
    return request, report


def _collect_context(
    session: AgentSession,
    registry: ToolRegistry,
    *,
    user_input: str | UserContent | None,
    cwd: Path | None,
    skill_registry: SkillRegistry | None,
    memory: MemoryStore | None,
    workflow_registry: WorkflowRegistry | None,
    extra_system_sections: Mapping[str, str] | None,
    now: datetime | None,
) -> _CollectedContext:
    resolved_cwd = (cwd or Path.cwd()).resolve()
    skill_listing = (
        skill_registry.prompt_listing()
        if skill_registry is not None
        else "No skills are currently available."
    )
    workflow_listing = (
        workflow_registry.prompt_listing()
        if workflow_registry is not None
        else "No workflows are currently available."
    )
    configured_user_data_dir = session.settings_snapshot.get("user_data_dir")
    prompt_user_data_dir = (
        configured_user_data_dir if isinstance(configured_user_data_dir, str) else "<user_data_dir>"
    )

    harness_system_text = SYSTEM_PROMPT_V1.format(
        environment=f"cwd: {resolved_cwd}",
        user_data_dir=prompt_user_data_dir,
        profiles=_profile_catalog_text(session),
        resources=_resource_catalog_text(session),
        skills=skill_listing,
        workflows=workflow_listing,
    )
    system_text = _prepend_soul(session, harness_system_text)
    canonical_user_input = (
        UserContent.text(user_input) if isinstance(user_input, str) else user_input
    )
    pending_images = (
        [part for part in canonical_user_input.parts if isinstance(part, ImagePart)]
        if canonical_user_input is not None
        else []
    )
    pending_image_count = len(pending_images)
    pending_image_bytes = sum(part.artifact.byte_count for part in pending_images)
    pending_image_pixels = sum(
        part.artifact.width * part.artifact.height for part in pending_images
    )
    media_settings = ContextSettings.model_validate(
        session.settings_snapshot.get("context", {})
    ).media
    image_limit = media_settings.request_image_limit
    if pending_image_count > image_limit:
        raise ValueError("pending user input exceeds the request image limit")
    if pending_image_bytes > media_settings.request_image_byte_limit:
        raise ValueError("pending user input exceeds the request image byte limit")
    if pending_image_pixels > media_settings.request_image_pixel_limit:
        raise ValueError("pending user input exceeds the request image pixel limit")
    projected_history, history_contributions = _project_history(
        session,
        image_slots=image_limit - pending_image_count,
        image_byte_budget=(media_settings.request_image_byte_limit - pending_image_bytes),
        image_pixel_budget=(media_settings.request_image_pixel_limit - pending_image_pixels),
    )
    system_message = Message.text("system", system_text)
    system_message.content.append(TextPart(text=_current_datetime_text(session, now)))
    messages = [system_message, *projected_history]
    contributions = [
        ("base_system_prompt", _part_chars(messages[0].content[0]), 1),
        ("base_system_prompt", _part_chars(messages[0].content[1]), 1),
        *history_contributions,
    ]

    if memory is not None:
        memory_text = memory.render_index(memory.index_char_limit)
        if memory_text is not None:
            message = Message.text("system", memory_text)
            messages.append(message)
            contributions.append(("memory_index", _part_chars(message.content[0]), 1))

    if session.active_skill is not None:
        skill_text = _active_skill_text(session.active_skill)
        message = Message.text("system", skill_text)
        messages.append(message)
        contributions.append(("active_skill", _part_chars(message.content[0]), 1))

    for name, text in (extra_system_sections or {}).items():
        message = Message.text("system", text)
        messages.append(message)
        contributions.append((name, _part_chars(message.content[0]), 1))

    if canonical_user_input is not None:
        message = Message(role="user", content=list(canonical_user_input.parts))
        messages.append(message)
        contributions.extend(
            (
                "pending_user_images" if isinstance(part, ImagePart) else "pending_user_input",
                _part_chars(part),
                1,
            )
            for part in message.content
        )

    tools = registry.specs()
    contributions.append(
        (
            "advertised_tool_definitions",
            sum(_serialized_chars(tool) for tool in tools),
            len(tools),
        )
    )
    return _CollectedContext(
        messages=messages,
        tools=tools,
        contributions=contributions,
        pending_user_input_included=canonical_user_input is not None,
    )


def _current_datetime_text(session: AgentSession, now: datetime | None) -> str:
    """Render the current instant in UTC and the session IANA timezone."""
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("context datetime must be timezone-aware")
    utc = instant.astimezone(UTC)
    local = utc.astimezone(ZoneInfo(session.timezone))
    utc_text = utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    return (
        "Current datetime:\n"
        f"- UTC: {utc_text}\n"
        f"- Session local: {local.isoformat(timespec='seconds')}\n"
        f"- Session timezone: {session.timezone}\n"
        "Interpret relative dates and times in the session timezone unless the user "
        "specifies another timezone."
    )


def _prepend_soul(session: AgentSession, harness_system_text: str) -> str:
    """Prepend shared then primary identity while preserving harness instructions."""
    configured_roots = session.settings_snapshot.get("profile_data_roots")
    if not isinstance(configured_roots, dict):
        return harness_system_text

    profiles = [SHARED_PROFILE]
    if session.profile_scope.primary != SHARED_PROFILE:
        profiles.append(session.profile_scope.primary)
    souls: list[str] = []
    for profile in profiles:
        configured_root = configured_roots.get(profile)
        if not isinstance(configured_root, str):
            continue
        soul_path = Path(configured_root) / SOUL_FILENAME
        try:
            soul = soul_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        if soul:
            souls.append(soul)

    if not souls:
        return harness_system_text
    normalized_harness = harness_system_text.lstrip("\n")
    return "\n\n".join((*souls, normalized_harness))


def _profile_catalog_text(session: AgentSession) -> str:
    """Render only the compact routing metadata issued to this session."""

    raw_definitions = session.settings_snapshot.get("profile_definitions")
    definitions = raw_definitions if isinstance(raw_definitions, dict) else {}
    lines: list[str] = []
    for profile in session.profile_scope.profiles:
        marker = (
            " (primary; default for new data)" if profile == session.profile_scope.primary else ""
        )
        raw = definitions.get(profile)
        definition = raw if isinstance(raw, dict) else {}
        description = definition.get("description")
        summary = description.strip() if isinstance(description, str) else ""
        line = f"- {profile}{marker}"
        if summary:
            line += f": {summary}"
        lines.append(line)
        routing_hints = definition.get("routing_hints")
        if isinstance(routing_hints, list):
            for hint in routing_hints:
                if isinstance(hint, str) and hint.strip():
                    lines.append(f"  - Routing hint: {hint.strip()}")
    return "\n".join(lines)


def _resource_catalog_text(session: AgentSession) -> str:
    """Render non-secret resource identities already filtered to the session scope."""

    raw_accounts = session.settings_snapshot.get("google_accounts")
    accounts = raw_accounts if isinstance(raw_accounts, dict) else {}
    if not accounts:
        return "No profile-owned account resources are currently available."
    lines = ["Google accounts (use the exact id as the `account` argument):"]
    for account_id in sorted(accounts):
        raw = accounts[account_id]
        account = raw if isinstance(raw, dict) else {}
        email = account.get("email")
        identity = email.strip() if isinstance(email, str) else ""
        suffix = f" — {identity}" if identity else ""
        lines.append(f"- {account_id}{suffix}")
    return "\n".join(lines)


def _identity_projection(collected: _CollectedContext) -> _CollectedContext:
    """Project already-selected history without further reduction."""
    return collected


def _project_history(
    session: AgentSession,
    *,
    image_slots: int,
    image_byte_budget: int,
    image_pixel_budget: int,
) -> tuple[list[Message], list[tuple[str, int, int]]]:
    checkpoint = session.active_checkpoint()
    if checkpoint is None:
        projected = _project_image_parts(
            session.history,
            image_slots=image_slots,
            image_byte_budget=image_byte_budget,
            image_pixel_budget=image_pixel_budget,
        )
        return projected, _history_contributions(projected)

    boundary = checkpoint.retained_from_message
    if boundary > len(session.history):
        raise ValueError("active checkpoint boundary exceeds session history")
    actual_digest = source_history_digest(session.history[:boundary])
    if actual_digest != checkpoint.source_digest:
        raise ValueError(f"active checkpoint {checkpoint.id} source history digest mismatch")

    marker = Message.text(
        "user",
        (
            f"<historical_context_checkpoint id={json.dumps(checkpoint.id)} "
            f"covered_messages={checkpoint.covered_message_count}>\n"
            "The following assistant message is a historical summary and "
            "deterministic observed state.\n"
            "</historical_context_checkpoint>"
        ),
    )
    observed = json.dumps(
        checkpoint.observed.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    summary = Message.text(
        "assistant",
        (
            "Historical checkpoint summary (quoted conversation data):\n\n"
            f"{checkpoint.summary}\n\n"
            "Deterministically observed state:\n"
            f"```json\n{observed}\n```"
        ),
    )
    checkpoint_contributions = [
        ("checkpoint_summary", _part_chars(part), 1)
        for message in (marker, summary)
        for part in message.content
    ]
    tail = _project_image_parts(
        session.history[boundary:],
        image_slots=image_slots,
        image_byte_budget=image_byte_budget,
        image_pixel_budget=image_pixel_budget,
    )
    return [marker, summary, *tail], [
        *checkpoint_contributions,
        *_history_contributions(tail),
    ]


def _history_contributions(messages: list[Message]) -> list[tuple[str, int, int]]:
    contributions: list[tuple[str, int, int]] = []
    for message in messages:
        for part in message.content:
            if isinstance(part, TextPart):
                name = "conversation_text"
            elif isinstance(part, ImagePart):
                name = "conversation_images"
            elif isinstance(part, ThinkingPart):
                name = "assistant_thinking"
            elif isinstance(part, ToolCallPart):
                name = "tool_calls"
            elif isinstance(part, ToolResultPart):
                name = "tool_results"
            else:  # pragma: no cover - ContentPart is a closed discriminated union.
                continue
            contributions.append((name, _part_chars(part), 1))
    return contributions


def _project_image_parts(
    messages: list[Message],
    *,
    image_slots: int,
    image_byte_budget: int,
    image_pixel_budget: int,
) -> list[Message]:
    """Keep the newest history suffix that fits every request image ceiling."""
    remaining_slots = image_slots
    remaining_bytes = image_byte_budget
    remaining_pixels = image_pixel_budget
    keep: set[tuple[int, int]] = set()
    exhausted = False
    for message_index in range(len(messages) - 1, -1, -1):
        message = messages[message_index]
        for part_index in range(len(message.content) - 1, -1, -1):
            part = message.content[part_index]
            if not isinstance(part, ImagePart):
                continue
            pixels = part.artifact.width * part.artifact.height
            if (
                remaining_slots < 1
                or part.artifact.byte_count > remaining_bytes
                or pixels > remaining_pixels
            ):
                exhausted = True
                break
            keep.add((message_index, part_index))
            remaining_slots -= 1
            remaining_bytes -= part.artifact.byte_count
            remaining_pixels -= pixels
        if exhausted:
            break

    projected: list[Message] = []
    for message_index, message in enumerate(messages):
        content = []
        for part_index, part in enumerate(message.content):
            if isinstance(part, ImagePart) and (message_index, part_index) not in keep:
                artifact = part.artifact
                content.append(
                    TextPart(
                        text=(
                            "[older image omitted from this request: "
                            f"{artifact.id}, {artifact.width}x{artifact.height}, "
                            f"{artifact.byte_count} bytes, sha256 {artifact.sha256[:12]}…]"
                        )
                    )
                )
            else:
                content.append(part)
        projected.append(message.model_copy(update={"content": content}, deep=True))
    return projected


def _account_request(
    session: AgentSession,
    request: CompletionRequest,
    contributions: list[tuple[str, int, int]],
    pending_user_input_included: bool,
) -> ContextReport:
    settings = ContextSettings.model_validate(session.settings_snapshot.get("context", {}))
    serialized_chars = len(serialize_canonical_request(request))
    image_parts = [
        part
        for message in request.messages
        for part in message.content
        if isinstance(part, ImagePart)
    ]
    projected_image_bytes = sum(part.artifact.byte_count for part in image_parts)
    projected_image_pixels = sum(part.artifact.width * part.artifact.height for part in image_parts)
    if len(image_parts) > settings.media.request_image_limit:
        raise ValueError("context exceeds the request image count limit")
    if projected_image_bytes > settings.media.request_image_byte_limit:
        raise ValueError("context exceeds the request image byte limit")
    if projected_image_pixels > settings.media.request_image_pixel_limit:
        raise ValueError("context exceeds the request image pixel limit")
    estimated_image_tokens = len(image_parts) * session.model_context.image_token_estimate
    totals: dict[str, tuple[int, int]] = {name: (0, 0) for name in _BASE_SECTIONS}
    extra_order: list[str] = []
    for name, chars, count in contributions:
        if name not in totals:
            totals[name] = (0, 0)
            extra_order.append(name)
        prior_chars, prior_count = totals[name]
        totals[name] = (prior_chars + chars, prior_count + count)

    accounted_chars = sum(chars for chars, _ in totals.values())
    envelope_chars = serialized_chars - accounted_chars
    if envelope_chars < 0:
        raise RuntimeError("context section accounting exceeded canonical request size")
    totals["canonical_request_envelope_overhead"] = (
        envelope_chars,
        len(request.messages) + 1,
    )

    ordered_names = [
        *_BASE_SECTIONS[:7],
        *extra_order,
        *_BASE_SECTIONS[7:],
        "canonical_request_envelope_overhead",
    ]
    sections = [
        ContextSection(
            name=name,
            chars=totals[name][0],
            estimated_tokens=(
                _estimate_tokens(totals[name][0], settings.chars_per_token)
                + (
                    totals[name][1] * session.model_context.image_token_estimate
                    if name in {"conversation_images", "pending_user_images"}
                    else 0
                )
            ),
            item_count=totals[name][1],
        )
        for name in ordered_names
    ]
    estimated_input_tokens = (
        _estimate_tokens(serialized_chars, settings.chars_per_token) + estimated_image_tokens
    )
    budget = _context_budget(
        session,
        settings,
        estimated_input_tokens=estimated_input_tokens,
        requested_output_tokens=request.max_tokens,
    )
    artifact_refs = [
        part.artifact
        for message in request.messages
        for part in message.content
        if isinstance(part, ToolResultPart) and part.artifact is not None
    ]
    checkpoint = session.active_checkpoint()
    checkpoint_report = None
    if checkpoint is not None:
        original_artifact_ids = {
            part.artifact.id
            for message in session.history
            for part in message.content
            if isinstance(part, ToolResultPart) and part.artifact is not None
        }
        checkpoint_report = CheckpointContextReport(
            id=checkpoint.id,
            created_at=checkpoint.created_at,
            covered_raw_messages=checkpoint.covered_message_count,
            retained_raw_messages=len(session.history) - checkpoint.retained_from_message,
            original_history_messages=len(session.history),
            summary_chars=len(checkpoint.summary),
            estimated_tokens_before=checkpoint.estimated_tokens_before,
            estimated_tokens_after=checkpoint.estimated_tokens_after,
            estimated_token_reduction=(
                checkpoint.estimated_tokens_before - checkpoint.estimated_tokens_after
            ),
            artifact_reference_count=len(original_artifact_ids),
        )
    return ContextReport(
        sections=sections,
        serialized_chars=serialized_chars,
        estimated_input_tokens=estimated_input_tokens,
        message_count=len(request.messages),
        tool_count=len(request.tools),
        artifact_count=len(artifact_refs),
        stored_artifact_chars=sum(ref.full_chars for ref in artifact_refs),
        retained_image_count=sum(
            isinstance(part, ImagePart) for message in session.history for part in message.content
        ),
        projected_image_count=len(image_parts),
        projected_image_bytes=projected_image_bytes,
        projected_image_pixels=projected_image_pixels,
        estimated_image_tokens=estimated_image_tokens,
        checkpoint=checkpoint_report,
        budget=budget,
        pending_user_input_included=pending_user_input_included,
    )


def _context_budget(
    session: AgentSession,
    settings: ContextSettings,
    *,
    estimated_input_tokens: int,
    requested_output_tokens: int | None,
) -> ContextBudget:
    if requested_output_tokens is not None:
        output_reserve = max(0, requested_output_tokens)
    elif session.model_context.max_output_tokens is not None:
        output_reserve = min(
            session.model_context.max_output_tokens,
            settings.response_reserve_tokens,
        )
    else:
        output_reserve = settings.response_reserve_tokens

    context_window = session.model_context.context_window_tokens
    if context_window is None:
        hard_input = None
        remaining = None
    else:
        hard_input = max(
            0,
            context_window - output_reserve - settings.safety_margin_tokens,
        )
        remaining = hard_input - estimated_input_tokens
    return ContextBudget(
        context_window_tokens=context_window,
        output_reserve_tokens=output_reserve,
        safety_margin_tokens=settings.safety_margin_tokens,
        hard_input_tokens=hard_input,
        remaining_tokens=remaining,
        capacity_source=session.model_context.source,
    )


def _estimate_tokens(chars: int, chars_per_token: float) -> int:
    return math.ceil(chars / chars_per_token)


def _serialized_chars(value: object) -> int:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _part_chars(part: object) -> int:
    return _serialized_chars(part)


def _active_skill_text(skill: ActiveSkill) -> str:
    args = skill.args if skill.args else "(none)"
    resources = (
        "\nSupporting resources named by this skill are bundle-relative. "
        "Read them with read_skill_resource when needed.\n"
        if skill.bundle_path is not None
        else ""
    )
    return f"""Active skill: {skill.qualified_name}
Args: {args}

Apply this skill to the current task. Build or update the task list from any
sequence in the skill, then follow its guardrails and success criteria.
{resources}

Skill instructions:
{skill.body}
"""
