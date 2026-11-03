"""Turn-owned selection of native tool definitions without changing authority."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ricky.tools.base import Risk, ToolContext, ToolResult
from ricky.tools.registry import ToolRegistry

ToolLoadingProfile = Literal["chat", "gateway"]

TOOL_LOADING_RECOVERY = (
    "Repeated tool-loading requests added no callable tools. Use an already available "
    "native tool definition, select a genuinely missing tool from the catalog, or explain "
    "the concrete blocker. Reloading does not change schemas or fix invalid arguments; "
    "correct arguments using the existing definition and reported error. Another "
    "loading-only response that adds no tools will stop this turn."
)
MAX_NO_PROGRESS_LOADING_ROUNDS = 3

CHAT_BASELINE = frozenset(
    {
        "run_shell",
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "glob_search",
        "grep_search",
        "update_tasks",
        "use_skill",
        "read_skill_resource",
        "search_skill_resources",
    }
)
GATEWAY_BASELINE = frozenset(
    {
        "delegate_task",
        "create_durable_task",
        "read_durable_task",
        "search_durable_tasks",
        "prepare_capability_use",
        "start_named_job",
        "list_execution_requests",
        "read_execution_request",
        "cancel_execution_request",
        "update_tasks",
        "use_skill",
        "read_skill_resource",
        "search_skill_resources",
    }
)


class LoadToolsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    names: list[str] = Field(
        min_length=1,
        max_length=128,
        description="Exact tool names from the catalog; load related tools together.",
    )


class ToolLoadingState:
    """One turn's loaded names; never grants permissions or survives the turn."""

    def __init__(self, profile: ToolLoadingProfile) -> None:
        self.baseline = CHAT_BASELINE if profile == "chat" else GATEWAY_BASELINE
        self.loaded: set[str] = set()

    def registry(self, eligible: ToolRegistry) -> ToolRegistry:
        # Retain registry order and original wrapped instances. Internal artifact
        # readers retain their existing availability rule once an artifact exists.
        selected = [
            tool
            for tool in eligible.tools()
            if tool.name in self.baseline
            or tool.name in self.loaded
            or getattr(tool, "deferred_until_artifact", False)
        ]
        return eligible.with_tools([*selected, LoadToolsTool(self, eligible)])

    def catalog(self, eligible: ToolRegistry) -> str:
        lines = [
            "Tool discovery catalog (names and summaries, not callable schemas).",
            "Tools with native definitions are callable now. For other tools, call "
            "load_tools with their exact names, then wait for the next response before "
            "calling them. Load related tools together. Loading grants no permission. "
            "Loaded definitions remain available for this user turn only; reload as "
            "needed on later turns. Do not reload tools already available in this turn. "
            "Reloading does not change schemas or fix invalid arguments; correct arguments "
            "using the existing definition and reported error. "
            "Skills may suggest tools but do not load them.",
        ]
        for tool in eligible.tools():
            if tool.name in self.baseline or getattr(tool, "deferred_until_artifact", False):
                continue
            summary = " ".join(tool.description.split())
            if len(summary) > 160:
                summary = summary[:157].rsplit(" ", 1)[0] + "…"
            lines.append(f"- {tool.name}: {summary}")
        return "\n".join(lines)


class LoadToolsTool:
    """Expose selected schemas starting with the next model request."""

    name: ClassVar[str] = "load_tools"
    description: ClassVar[str] = (
        "Load native argument schemas for catalog tools needed in this turn."
    )
    Params: ClassVar[type[BaseModel]] = LoadToolsParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.session.mutate"
    effect_kind = "ricky_state"
    unattended = "forbidden"
    state_guard_id = None

    def __init__(self, state: ToolLoadingState, eligible: ToolRegistry) -> None:
        self._state = state
        self._eligible = eligible

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = LoadToolsParams.model_validate(params)
        names = list(dict.fromkeys(args.names))
        unavailable = [name for name in names if self._eligible.get(name) is None]
        if unavailable:
            return ToolResult(
                content="No tools loaded. Unavailable tools: "
                + ", ".join(unavailable)
                + ". Choose exact names from the current tool catalog.",
                is_error=True,
            )
        available = {tool.name for tool in self._state.registry(self._eligible).tools()}
        newly_loaded = [name for name in names if name not in available]
        already_available = [name for name in names if name in available]
        # No await between validation and mutation: a batch is all-or-nothing.
        self._state.loaded.update(names)
        feedback: list[str] = []
        if newly_loaded:
            feedback.append(
                "Newly loaded schemas available on the next request: "
                + ", ".join(newly_loaded)
                + "."
            )
        if already_available:
            feedback.append(
                "Already available: "
                + ", ".join(already_available)
                + ". Invoke these tools directly using their native definitions."
            )
        feedback.append(
            "Reloading does not change schemas or fix invalid arguments; correct arguments "
            "using the existing definition and reported error. Existing permissions still apply."
        )
        return ToolResult(content=" ".join(feedback))
