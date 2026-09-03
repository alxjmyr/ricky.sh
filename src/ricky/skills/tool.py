"""Model-callable skill activation tool."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ricky.skills.registry import SkillRegistry
from ricky.tools.base import Risk, ToolContext, ToolResult


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UseSkillParams(_Params):
    """Arguments for use_skill."""

    name: str = Field(description="Skill name to activate.")
    args: str = Field(default="", description="Optional user-visible arguments for the skill.")


class UseSkillTool:
    """Activate a prompt skill for subsequent model requests."""

    name: ClassVar[str] = "use_skill"
    description: ClassVar[str] = (
        "Activate one loaded prompt skill by name so its instructions shape subsequent requests."
    )
    Params: ClassVar[type[BaseModel]] = UseSkillParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.skill.use"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, skill_registry: SkillRegistry) -> None:
        self._skill_registry = skill_registry

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        """Activate the requested skill on the current session."""
        parsed = UseSkillParams.model_validate(params)
        activation = self._skill_registry.activate(ctx.session, parsed.name, parsed.args)
        if not activation.ok:
            return ToolResult(content=activation.error or "skill activation failed", is_error=True)

        assert activation.skill is not None
        replaced = (
            f" Replaced active skill '{activation.previous_skill}'."
            if activation.previous_skill is not None
            else ""
        )
        args = f" Args: {activation.skill.args}" if activation.skill.args else ""
        return ToolResult(
            content=(f"Activated skill '{activation.skill.qualified_name}'.{args}{replaced}")
        )


class ReadSkillResourceParams(_Params):
    """Arguments for reading a passive active-skill resource."""

    path: str = Field(description="Path relative to the active skill bundle.")
    offset: int = Field(default=1, ge=1, description="First 1-based line number to include.")
    limit: int = Field(default=200, ge=1, le=1000, description="Maximum lines to include.")


class ReadSkillResourceTool:
    """Read passive text from the active skill bundle."""

    name: ClassVar[str] = "read_skill_resource"
    description: ClassVar[str] = (
        "Read a text reference or template named by the active skill. "
        "Paths are relative to that skill's bundle."
    )
    Params: ClassVar[type[BaseModel]] = ReadSkillResourceParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.skill.use"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, skill_registry: SkillRegistry) -> None:
        self._skill_registry = skill_registry

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        """Read numbered lines from one bundle-scoped passive resource."""
        parsed = ReadSkillResourceParams.model_validate(params)
        active_skill = ctx.session.active_skill
        if active_skill is None:
            return ToolResult(
                content="No skill is active; activate a bundled skill before reading resources.",
                is_error=True,
            )
        try:
            path = self._skill_registry.resolve_resource(active_skill, parsed.path)
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)

        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = parsed.offset - 1
        selected = lines[start : start + parsed.limit]
        content = "\n".join(f"{start + index + 1}: {line}" for index, line in enumerate(selected))
        return ToolResult(content=content or "[empty]")
