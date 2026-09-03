"""Claude Code CLI provider adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ricky.config import RickySettings, user_data_path
from ricky.llm.provider import MediaResolver
from ricky.llm.types import (
    AuthError,
    CompletionRequest,
    ImagePart,
    Message,
    MessageDone,
    ModelInfo,
    ProviderError,
    RateLimitError,
    TextDelta,
    TextPart,
    ThinkingDelta,
    ThinkingPart,
    ToolCallDelta,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    TransportError,
    UnsupportedInputModalityError,
    Usage,
)

_TOOL_FENCE = "```tool_call"
_FENCE_END = "```"
_MALFORMED_TOOL_NAME = "__ricky_malformed_tool_call__"
_STDERR_TAIL_BYTES = 8192
_SUBPROCESS_STREAM_LIMIT = 10 * 1024 * 1024
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_IMAGE_PLACEHOLDER = re.compile(r"\[\[RICKY_IMAGE:(media_[0-9a-f]{32})\]\]")
_CONTINUATION = (
    "Inspect the latest tool result(s) and continue the original task. "
    "If requested work remains, call the necessary tools now. Do not stop "
    "at a promise, progress update, or statement of the next action. Finish "
    "only when the requested outcome is verified complete or you are "
    "concretely blocked."
)
_EMPTY_RESPONSE_CONTINUATION = (
    "Continue the current user turn now. Either call the next required tools "
    "or provide a non-empty final answer."
)


@dataclass(frozen=True)
class _ParsedToolCall:
    id: str
    name: str
    args: dict[str, Any]
    argument_error: Literal["malformed JSON", "non-object JSON"] | None = None


class ClaudeCodeSessionState(BaseModel):
    """Persisted provider-native state for one Ricky agent session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claude_session_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    working_directory: str
    model: str | None = None
    post_compaction: bool = False
    conversation_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _SessionInvocation:
    state_path: Path
    working_directory: Path
    claude_session_id: str
    resumed: bool
    post_compaction: bool
    conversation_digest: str


def render_tool_protocol(tools: list[ToolSpec]) -> str:
    """Render canonical tool specifications into Claude's prompt protocol."""
    rendered = []
    for tool in tools:
        schema = json.dumps(
            tool.parameters,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        rendered.append(f"- {tool.name}: {tool.description}\n  parameters: {schema}")
    catalog = "\n".join(rendered)
    return f"""<ricky_tool_protocol>
You may request only the tools listed below. Ricky, not Claude Code, executes
them after applying its permission policy.

{catalog}

To request a tool, end your reply with one fenced block per call. The opening
and closing markers must each be on their own line. Parallel calls use
multiple consecutive blocks. Do not write anything after the final block.

{_TOOL_FENCE}
{{"name":"tool_name","args":{{"argument":"value"}}}}
{_FENCE_END}
</ricky_tool_protocol>"""


def render_tool_call(call: ToolCallPart) -> str:
    """Render one canonical tool call for transcript replay."""
    payload = json.dumps(
        {"name": call.name, "args": call.args},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{_TOOL_FENCE}\n{payload}\n{_FENCE_END}"


def to_claude_prompt(request: CompletionRequest) -> tuple[str, str]:
    """Render a canonical request as Claude Code system text and stdin prompt."""
    system_parts: list[str] = []
    conversation: list[Message] = []
    for message in request.messages:
        if message.role == "system":
            system_parts.extend(part.text for part in message.content if isinstance(part, TextPart))
        else:
            conversation.append(message)

    if request.tools:
        system_parts.append(render_tool_protocol(request.tools))
    system_prompt = "\n\n".join(system_parts)

    if not conversation:
        return system_prompt, ""

    if conversation[-1].role == "user":
        live_prompt = _render_user_content(conversation[-1])
        history = conversation[:-1]
        if not history:
            return system_prompt, live_prompt
        return system_prompt, _with_history(history, f"[user] {live_prompt}")

    return system_prompt, _with_history(conversation, _CONTINUATION)


def to_claude_delta_prompt(
    request: CompletionRequest,
    *,
    repeated_conversation: bool = False,
) -> tuple[str, str]:
    """Render only content Claude Code has not retained in its resumed session."""
    system_prompt, _ = to_claude_prompt(request)
    conversation = [message for message in request.messages if message.role != "system"]
    if not conversation:
        return system_prompt, ""
    if repeated_conversation:
        return system_prompt, _EMPTY_RESPONSE_CONTINUATION
    if conversation[-1].role == "user":
        return system_prompt, _render_user_content(conversation[-1])

    trailing_results: list[Message] = []
    for message in reversed(conversation):
        if message.role != "tool":
            break
        trailing_results.append(message)
    trailing_results.reverse()
    if trailing_results:
        rendered = "\n".join(_render_message(message) for message in trailing_results)
        return system_prompt, f"{rendered}\n\n{_CONTINUATION}"
    return system_prompt, _CONTINUATION


def parse_tool_call_suffix(
    text: str,
    *,
    id_factory: Callable[[], str] | None = None,
) -> list[_ParsedToolCall] | None:
    """Parse a strict, contiguous suffix of fenced tool-call blocks.

    A well-formed fence with malformed JSON becomes a deliberately unknown
    synthetic tool. An unterminated fence is accepted only when its JSON body
    parses successfully; otherwise the caller must preserve it as ordinary text.
    """
    make_id = id_factory or _new_call_id
    lines = text.splitlines(keepends=True)
    calls: list[_ParsedToolCall] = []
    index = 0

    while index < len(lines):
        while index < len(lines) and _line_value(lines[index]).strip() == "":
            index += 1
        if index >= len(lines):
            break
        if _line_value(lines[index]) != _TOOL_FENCE:
            return None
        index += 1

        content: list[str] = []
        terminated = False
        while index < len(lines):
            if _line_value(lines[index]) == _FENCE_END:
                terminated = True
                index += 1
                break
            content.append(lines[index])
            index += 1

        raw = "".join(content).strip()
        parsed = _parse_tool_object(raw, make_id, allow_malformed=terminated)
        if parsed is None:
            return None
        calls.append(parsed)

    return calls or None


class ToolProtocolParser:
    """Incrementally separate streamed prose from a strict tool-call suffix."""

    def __init__(self, *, id_factory: Callable[[], str] | None = None) -> None:
        self._id_factory = id_factory
        self._line_buffer = ""
        self._streamed_partial = ""
        self._candidate: str | None = None
        self._inside_block = False
        self._closed = False
        self.text = ""
        self.tool_calls: list[_ParsedToolCall] = []

    def feed(self, chunk: str) -> list[TextDelta]:
        """Consume a text chunk, yielding prose that is safe to expose live."""
        if self._closed:
            raise TransportError("Claude Code emitted text after its result envelope")
        self._line_buffer += chunk
        events: list[TextDelta] = []
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            events.extend(self._feed_line(f"{line}\n"))
        events.extend(self._stream_safe_partial())
        return events

    def _stream_safe_partial(self) -> list[TextDelta]:
        """Stream a partial line as soon as it can no longer open a tool fence."""
        if self._candidate is not None or not self._line_buffer:
            return []
        if _TOOL_FENCE.startswith(self._line_buffer.removesuffix("\r")):
            return []
        pending = self._line_buffer[len(self._streamed_partial) :]
        if not pending:
            return []
        self._streamed_partial = self._line_buffer
        return [TextDelta(delta=pending)]

    def finish(self) -> list[TextDelta | ToolCallDelta]:
        """Finish parsing and emit a buffered suffix as calls or ordinary text."""
        if self._closed:
            return []
        self._closed = True
        events: list[TextDelta | ToolCallDelta] = []
        if self._line_buffer:
            events.extend(self._feed_line(self._line_buffer))
            self._line_buffer = ""

        if self._candidate is None:
            return events

        candidate = self._candidate
        self._candidate = None
        calls = parse_tool_call_suffix(candidate, id_factory=self._id_factory)
        if calls is None:
            self.text += candidate
            events.append(TextDelta(delta=candidate))
            return events

        for index, call in enumerate(calls):
            self.tool_calls.append(call)
            events.append(
                ToolCallDelta(
                    index=index,
                    id=call.id,
                    name=call.name,
                    args_delta=json.dumps(
                        call.args,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
            )
        return events

    def _feed_line(self, line: str) -> list[TextDelta]:
        value = _line_value(line)
        if self._candidate is None:
            if value == _TOOL_FENCE:
                self._candidate = line
                self._inside_block = True
                return []
            self.text += line
            # A prefix of this line may already have streamed via
            # _stream_safe_partial; emit only the remainder.
            delta = line[len(self._streamed_partial) :]
            self._streamed_partial = ""
            return [TextDelta(delta=delta)] if delta else []

        self._candidate += line
        if self._inside_block:
            if value == _FENCE_END:
                self._inside_block = False
            return []
        if value == _TOOL_FENCE:
            self._inside_block = True
            return []
        if value.strip() == "":
            return []

        rejected = self._candidate
        self._candidate = None
        self._inside_block = False
        self.text += rejected
        return [TextDelta(delta=rejected)]


class ClaudeCodeProvider:
    """Completion adapter over a local Claude Code subscription."""

    name = "claude_code"

    def __init__(
        self,
        settings: RickySettings,
        *,
        media_resolver: MediaResolver | None = None,
    ) -> None:
        provider_settings = settings.providers.claude_code
        self._cli_path = provider_settings.cli_path
        self._resume_sessions = provider_settings.resume_sessions
        self._timeout = settings.request_timeout_seconds
        self._user_data_root = user_data_path(settings)
        self._cwd = Path(tempfile.mkdtemp(prefix="ricky-claude-code-"))
        self._processes: set[asyncio.subprocess.Process] = set()
        self._closed = False
        self._media_resolver = media_resolver

    def bind_media_resolver(self, resolver: MediaResolver) -> None:
        """Bind image materialization to this provider's exact session runtime."""
        self._media_resolver = resolver

    async def aclose(self) -> None:
        """Stop owned subprocesses and remove the neutral working directory."""
        if self._closed:
            return
        self._closed = True
        processes = list(self._processes)
        for process in processes:
            if process.returncode is None:
                process.kill()
        if processes:
            await asyncio.gather(*(process.wait() for process in processes))
        shutil.rmtree(self._cwd, ignore_errors=True)

    async def rotate(self, session_id: str) -> None:
        """Invalidate native history after Ricky changes its canonical projection."""
        if not self._resume_sessions:
            return
        state_path, working_directory = self._session_paths(session_id)
        prior = await _read_session_state(state_path)
        state = ClaudeCodeSessionState(
            working_directory=str(working_directory),
            model=prior.model if prior is not None else None,
            post_compaction=True,
        )
        await _write_session_state(state_path, state)

    async def list_models(self) -> list[ModelInfo]:
        """Return stable aliases that Claude Code resolves itself."""
        return [
            ModelInfo(id="fable", name="Fable (latest)", input_modalities=["text", "image"]),
            ModelInfo(id="opus", name="Opus (latest)", input_modalities=["text", "image"]),
            ModelInfo(id="sonnet", name="Sonnet (latest)", input_modalities=["text", "image"]),
            ModelInfo(id="haiku", name="Haiku (latest)", input_modalities=["text", "image"]),
        ]

    async def stream(
        self, request: CompletionRequest
    ) -> AsyncIterator[TextDelta | ThinkingDelta | ToolCallDelta | MessageDone]:
        """Run one isolated Claude Code process and translate its JSON stream."""
        if self._closed:
            raise ProviderError("Claude Code provider is closed")

        invocation = await self._prepare_session(request)
        if invocation is not None and invocation.resumed:
            prior = await _read_session_state(invocation.state_path)
            repeated = (
                prior is not None and prior.conversation_digest == invocation.conversation_digest
            )
            system_prompt, stdin_prompt = to_claude_delta_prompt(
                request,
                repeated_conversation=repeated,
            )
        else:
            system_prompt, stdin_prompt = to_claude_prompt(request)
        has_images = _contains_images(request)
        if has_images:
            stdin_prompt = await _stream_json_prompt(
                stdin_prompt,
                request,
                self._media_resolver,
            )
        working_directory = invocation.working_directory if invocation is not None else self._cwd
        environment = os.environ.copy()
        environment.pop("ANTHROPIC_API_KEY", None)
        environment.pop("ANTHROPIC_AUTH_TOKEN", None)
        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[str] | None = None
        session_committed = False

        try:
            # Inactivity guard: the deadline is pushed forward on every stdout
            # line, so steady streaming can outlast the timeout but a hung
            # subprocess (or a stalled prompt write) is killed after one
            # silent interval — matching the HTTP adapters' per-read timeouts.
            async with asyncio.timeout(self._timeout) as deadline:
                try:
                    arguments = [
                        "-p",
                        "--model",
                        request.model,
                        "--system-prompt",
                        system_prompt,
                        "--output-format",
                        "stream-json",
                        "--include-partial-messages",
                        "--verbose",
                        "--tools",
                        "",
                        "--setting-sources",
                        "",
                        "--strict-mcp-config",
                        "--max-turns",
                        "1",
                    ]
                    if has_images:
                        arguments.extend(("--input-format", "stream-json"))
                    if invocation is None:
                        arguments.append("--no-session-persistence")
                    elif invocation.resumed:
                        arguments.extend(("--resume", invocation.claude_session_id))
                    else:
                        arguments.extend(("--session-id", invocation.claude_session_id))
                    process = await asyncio.create_subprocess_exec(
                        self._cli_path,
                        *arguments,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=working_directory,
                        env=environment,
                        limit=_SUBPROCESS_STREAM_LIMIT,
                    )
                except FileNotFoundError as exc:
                    if shutil.which(self._cli_path) is None:
                        raise AuthError(
                            "Claude Code CLI was not found; install `claude` or set "
                            "providers.claude_code.cli_path"
                        ) from exc
                    raise TransportError(f"Failed to launch Claude Code: {exc}") from exc
                except OSError as exc:
                    raise TransportError(f"Failed to launch Claude Code: {exc}") from exc

                self._processes.add(process)
                assert process.stderr is not None
                stderr_task = asyncio.create_task(_read_stderr_tail(process.stderr))
                await _write_prompt(process, stdin_prompt)

                parser = ToolProtocolParser()
                thinking = ""
                done: MessageDone | None = None
                loop = asyncio.get_running_loop()
                assert process.stdout is not None
                async for raw_line in process.stdout:
                    deadline.reschedule(loop.time() + self._timeout)
                    payload = _loads_stream_line(raw_line)
                    payload_type = payload.get("type")

                    if payload_type == "stream_event":
                        event = payload.get("event")
                        if not isinstance(event, dict):
                            raise TransportError("Malformed Claude Code stream event")
                        delta = event.get("delta")
                        if event.get("type") != "content_block_delta" or not isinstance(
                            delta, dict
                        ):
                            continue
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            value = delta.get("text")
                            if not isinstance(value, str):
                                raise TransportError("Malformed Claude Code text delta")
                            for parsed_event in parser.feed(value):
                                yield parsed_event
                        elif delta_type == "thinking_delta":
                            value = delta.get("thinking")
                            if not isinstance(value, str):
                                raise TransportError("Malformed Claude Code thinking delta")
                            thinking += value
                            yield ThinkingDelta(delta=value)
                        continue

                    if payload_type != "result":
                        continue
                    if done is not None:
                        raise TransportError("Claude Code emitted multiple result envelopes")
                    if _is_error_result(payload):
                        _raise_result_error(payload)

                    for parsed_event in parser.finish():
                        yield parsed_event
                    usage = _usage_from_result(payload)
                    done = MessageDone(
                        message=_assembled_message(parser, thinking),
                        usage=usage,
                        stop_reason=_map_stop_reason(
                            payload.get("stop_reason"),
                            has_tool_calls=bool(parser.tool_calls),
                        ),
                    )

                return_code = await process.wait()
                stderr_tail = await stderr_task
                stderr_task = None
                if done is None:
                    detail = f": {stderr_tail}" if stderr_tail else ""
                    if return_code != 0:
                        raise TransportError(
                            f"Claude Code exited with status {return_code}{detail}"
                        )
                    raise TransportError(f"Claude Code stream ended without a result{detail}")
                if invocation is not None:
                    await _write_session_state(
                        invocation.state_path,
                        ClaudeCodeSessionState(
                            claude_session_id=invocation.claude_session_id,
                            working_directory=str(invocation.working_directory),
                            model=request.model,
                            post_compaction=invocation.post_compaction,
                            conversation_digest=invocation.conversation_digest,
                        ),
                    )
                    session_committed = True
                yield done
        except TimeoutError as exc:
            await _kill_process(process)
            raise TransportError(
                f"Claude Code request timed out after {self._timeout:g} seconds of inactivity"
            ) from exc
        except asyncio.CancelledError:
            await _kill_process(process)
            raise
        except BaseException:
            await _kill_process(process)
            raise
        finally:
            if process is not None:
                self._processes.discard(process)
            if stderr_task is not None:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
                await stderr_task
            if invocation is not None and invocation.resumed and not session_committed:
                await _remove_session_state(invocation.state_path)

    async def _prepare_session(
        self,
        request: CompletionRequest,
    ) -> _SessionInvocation | None:
        if not self._resume_sessions:
            return None
        raw_session_id = request.session_id
        if raw_session_id is None:
            return None

        state_path, working_directory = self._session_paths(raw_session_id)
        await asyncio.to_thread(working_directory.mkdir, parents=True, exist_ok=True)
        state = await _read_session_state(state_path)
        if state is not None and Path(state.working_directory) != working_directory:
            raise TransportError("Claude Code session state has an unexpected working directory")
        resumed = (
            state is not None
            and state.claude_session_id is not None
            and state.model == request.model
        )
        claude_session_id = (
            state.claude_session_id if resumed and state is not None else str(uuid4())
        )
        return _SessionInvocation(
            state_path=state_path,
            working_directory=working_directory,
            claude_session_id=cast(str, claude_session_id),
            resumed=resumed,
            post_compaction=state.post_compaction if state is not None else False,
            conversation_digest=_conversation_digest(request),
        )

    def _session_paths(self, session_id: str) -> tuple[Path, Path]:
        if _SESSION_ID.fullmatch(session_id) is None or session_id in {".", ".."}:
            raise TransportError("invalid Ricky session id for Claude Code state")
        root = self._user_data_root / "sessions" / session_id / "providers" / "claude-code"
        return root / "claude-code.json", root / "claude-code-workdir"


def _conversation_digest(request: CompletionRequest) -> str:
    payload = [
        message.model_dump(mode="json") for message in request.messages if message.role != "system"
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _read_session_state(path: Path) -> ClaudeCodeSessionState | None:
    try:
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TransportError(f"Failed to read Claude Code session state: {exc}") from exc
    try:
        return ClaudeCodeSessionState.model_validate_json(raw)
    except ValidationError as exc:
        raise TransportError("Malformed Claude Code session state") from exc


async def _write_session_state(path: Path, state: ClaudeCodeSessionState) -> None:
    try:
        await asyncio.to_thread(_write_session_state_sync, path, state.model_dump_json())
    except OSError as exc:
        raise TransportError(f"Failed to write Claude Code session state: {exc}") from exc


def _write_session_state_sync(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def _remove_session_state(path: Path) -> None:
    try:
        await asyncio.shield(asyncio.to_thread(path.unlink, missing_ok=True))
    except OSError as exc:
        raise TransportError(f"Failed to invalidate Claude Code session state: {exc}") from exc


def _with_history(history: list[Message], live_prompt: str) -> str:
    transcript = "\n".join(_render_message(message) for message in history)
    return f"<conversation_history>\n{transcript}\n</conversation_history>\n\n{live_prompt}"


def _render_user_content(message: Message) -> str:
    return "\n".join(
        part.text if isinstance(part, TextPart) else f"[[RICKY_IMAGE:{part.artifact.id}]]"
        for part in message.content
        if isinstance(part, (TextPart, ImagePart))
    )


def _render_message(message: Message) -> str:
    if message.role == "tool":
        results = []
        for part in message.content:
            if isinstance(part, ToolResultPart):
                status = " — error" if part.is_error else ""
                results.append(f"[tool_result for {part.call_id}{status}] {part.content}")
        return "\n".join(results)

    chunks: list[str] = []
    for part in message.content:
        if isinstance(part, TextPart):
            chunks.append(part.text)
        elif isinstance(part, ImagePart):
            chunks.append(f"[[RICKY_IMAGE:{part.artifact.id}]]")
        elif isinstance(part, ToolCallPart):
            chunks.append(render_tool_call(part))
    return f"[{message.role}] {'\n'.join(chunks)}"


def _contains_images(request: CompletionRequest) -> bool:
    return any(
        isinstance(part, ImagePart) for message in request.messages for part in message.content
    )


async def _stream_json_prompt(
    prompt: str,
    request: CompletionRequest,
    resolver: MediaResolver | None,
) -> str:
    """Encode one documented stream-json user message with ordered image blocks."""
    if resolver is None:
        raise UnsupportedInputModalityError(
            "Claude Code image input requires a bound session media resolver"
        )
    references = {
        part.artifact.id: part.artifact
        for message in request.messages
        for part in message.content
        if isinstance(part, ImagePart)
    }
    content: list[dict[str, Any]] = []
    position = 0
    for match in _IMAGE_PLACEHOLDER.finditer(prompt):
        if match.start() > position:
            content.append({"type": "text", "text": prompt[position : match.start()]})
        reference = references.get(match.group(1))
        if reference is None:
            raise TransportError("Claude Code image placeholder has no canonical reference")
        resolved = await resolver.resolve(reference)
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": resolved.media_type,
                    "data": base64.b64encode(resolved.content).decode("ascii"),
                },
            }
        )
        position = match.end()
    if position < len(prompt):
        content.append({"type": "text", "text": prompt[position:]})
    envelope = {
        "type": "user",
        "message": {
            "role": "user",
            "content": content,
        },
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n"


def _parse_tool_object(
    raw: str,
    id_factory: Callable[[], str],
    *,
    allow_malformed: bool,
) -> _ParsedToolCall | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        name = value.get("name")
        args = value.get("args")
        if isinstance(name, str) and name and isinstance(args, dict):
            return _ParsedToolCall(id=id_factory(), name=name, args=cast(dict[str, Any], args))
        if isinstance(name, str) and name:
            return _ParsedToolCall(
                id=id_factory(),
                name=name,
                args={},
                argument_error="non-object JSON",
            )
    if not allow_malformed:
        return None
    intended_name = _best_effort_name(raw)
    synthetic_args: dict[str, Any] = {}
    if intended_name is not None:
        synthetic_args["intended_name"] = intended_name
    return _ParsedToolCall(
        id=id_factory(),
        name=_MALFORMED_TOOL_NAME,
        args=synthetic_args,
        argument_error="malformed JSON",
    )


def _best_effort_name(raw: str) -> str | None:
    match = re.search(r'"name"\s*:\s*("(?:\\.|[^"\\])*")', raw)
    if match is None:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) and value else None


def _line_value(line: str) -> str:
    return line.removesuffix("\n").removesuffix("\r")


def _new_call_id() -> str:
    return f"call_{uuid4().hex[:8]}"


def _loads_stream_line(raw_line: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = raw_line.decode(errors="replace").strip()
        raise TransportError(f"Malformed Claude Code stream line: {preview}") from exc
    if not isinstance(payload, dict):
        raise TransportError("Unexpected Claude Code stream value")
    return payload


def _is_error_result(payload: dict[str, Any]) -> bool:
    subtype = payload.get("subtype")
    return payload.get("is_error") is True or (isinstance(subtype, str) and subtype != "success")


def _raise_result_error(payload: dict[str, Any]) -> None:
    raw_detail = payload.get("result") or payload.get("error") or payload.get("subtype")
    detail = raw_detail if isinstance(raw_detail, str) else json.dumps(raw_detail)
    lowered = detail.lower()
    if any(
        marker in lowered
        for marker in ("login", "authentication", "oauth", "unauthorized", "not logged")
    ):
        raise AuthError(f"Claude Code authentication failed: {detail}")
    if any(
        marker in lowered
        for marker in ("rate limit", "usage limit", "quota", "five-hour", "5-hour")
    ):
        raise RateLimitError(f"Claude Code usage limit exceeded: {detail}")
    if "image" in lowered and any(
        marker in lowered for marker in ("unsupported", "not support", "modality")
    ):
        raise UnsupportedInputModalityError(
            f"Claude Code model does not support image input: {detail}"
        )
    raise ProviderError(f"Claude Code request failed: {detail}")


def _usage_from_result(payload: dict[str, Any]) -> Usage:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return Usage()
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    return Usage(
        prompt_tokens=input_tokens if isinstance(input_tokens, int) else 0,
        completion_tokens=output_tokens if isinstance(output_tokens, int) else 0,
    )


def _map_stop_reason(reason: Any, *, has_tool_calls: bool) -> str | None:
    if has_tool_calls:
        return "tool_calls"
    if not isinstance(reason, str):
        return None
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
    }.get(reason, reason)


def _assembled_message(parser: ToolProtocolParser, thinking: str) -> Message:
    content = []
    if thinking:
        content.append(ThinkingPart(text=thinking))
    if parser.text:
        content.append(TextPart(text=parser.text))
    content.extend(
        ToolCallPart(
            id=call.id,
            name=call.name,
            args=call.args,
            argument_error=call.argument_error,
        )
        for call in parser.tool_calls
    )
    return Message(role="assistant", content=content)


async def _write_prompt(process: asyncio.subprocess.Process, prompt: str) -> None:
    assert process.stdin is not None
    try:
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise TransportError("Claude Code closed stdin before reading the prompt") from exc


async def _read_stderr_tail(stream: asyncio.StreamReader) -> str:
    tail = b""
    while chunk := await stream.read(4096):
        tail = (tail + chunk)[-_STDERR_TAIL_BYTES:]
    return tail.decode(errors="replace").strip()


async def _kill_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.kill()
    await process.wait()
