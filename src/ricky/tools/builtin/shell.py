"""Shell execution tool."""

from __future__ import annotations

import asyncio
import hashlib
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ricky.permissions import GrantScope
from ricky.tools.base import EffectReceipt, Risk, ToolContext, ToolResult, make_effect_identity


class RunShellParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(description="Shell command to run from the workspace root.")
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description="Optional timeout override in seconds.",
    )


class RunShellTool:
    name: ClassVar[str] = "run_shell"
    description: ClassVar[str] = "Run a shell command in the workspace and capture stdout/stderr."
    Params: ClassVar[type[BaseModel]] = RunShellParams
    risk: ClassVar[Risk] = "destructive"
    capability_id = "builtin.host.execute"
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def effect_identity(self, args: dict[str, object], ctx: ToolContext):
        command = str(args.get("command", ""))
        command_digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
        return make_effect_identity(
            operation=self.name,
            target=str(ctx.cwd.resolve()),
            occurrence=f"{ctx.session.id}:{command_digest}",
            summary=f"Run shell command in {ctx.cwd}",
        )

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        """Allow the user to authorize all shell calls for this session."""
        del args, ctx
        return GrantScope(
            label="run_shell commands",
            allow_unconstrained=True,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = RunShellParams.model_validate(params)
        timeout = args.timeout_seconds or ctx.settings.shell_timeout_seconds
        process = await asyncio.create_subprocess_shell(
            args.command,
            cwd=ctx.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult(
                content=f"Command timed out after {timeout:g}s",
                is_error=True,
                effect_receipt=EffectReceipt(disposition="performed"),
            )
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        content = (
            f"exit_code: {process.returncode}\n"
            f"stdout:\n{stdout or '[empty]'}\n"
            f"stderr:\n{stderr or '[empty]'}"
        )
        return ToolResult(
            content=content,
            is_error=process.returncode != 0,
            effect_receipt=EffectReceipt(
                disposition="performed",
                provider_reference=f"exit:{process.returncode}",
            ),
        )
