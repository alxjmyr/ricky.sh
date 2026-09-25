"""Automatic verification hold tools, with separately accounted input effects."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ricky.browser.holds import HOLD_EFFECT_TOOLS, BrowserHoldStatus
from ricky.browser.service import BrowserService
from ricky.browser.types import BrowserActionTarget, BrowserError
from ricky.permissions.types import Policy, PolicyRule
from ricky.tools import EffectIdentity, EffectReceipt, ToolContext, ToolResult, make_effect_identity


def verification_policy(policy: Policy) -> Policy:
    """Add default maintenance authority after the owner's explicit ordered rules."""
    return policy.model_copy(
        update={
            "rules": [
                *policy.rules,
                *(
                    PolicyRule(
                        tool_name=name,
                        decision="allow",
                        reason="automatic bounded browser verification",
                    )
                    for name in sorted(HOLD_EFFECT_TOOLS)
                ),
            ]
        }
    )


class HoldStartParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: BrowserActionTarget


class HoldParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hold_id: str = Field(pattern=r"^browser_hold_[0-9a-f]{32}$")


def _result(status: BrowserHoldStatus, *, effect: bool = False) -> ToolResult:
    if status.state == "holding":
        guidance = (
            "Next call browser_visual_snapshot to inspect current feedback while holding. "
            "Unchanged feedback or processing dots are not completion or rejection: keep "
            "observing within the remaining deadline. Release on explicit completion, "
            "rejection, an instruction to release, or observation failure. The runtime "
            "enforces the deadline independently; status polling is not visual observation."
        )
    elif status.state == "released":
        guidance = (
            "Next call browser_visual_snapshot on this page before concluding success or "
            "failure. A deadline release is an input limit, not a verification verdict. "
            "Semantic homepage content can remain behind an overlay and cannot prove "
            "clearance. If the latest image shows processing, take bounded fresh visual "
            "observations without repeating input; report unresolved if it does not settle. "
            "If the challenge is gone, continue the original authorized task using fresh "
            "targets and verify its result separately. Returning to a homepage rather than "
            "the requested results does not itself mean verification failed."
        )
    else:
        guidance = (
            "Input release is uncertain. Do not start another hold or browser action. "
            "Report the uncertain release; do not infer verification success or failure."
        )
    return ToolResult(
        content=(
            f"Verification input: {status.model_dump_json()}. "
            f"Input state is not proof that verification passed. {guidance}"
        ),
        data=status.model_dump(mode="json"),
        effect_receipt=(EffectReceipt(disposition="performed") if effect else None),
    )


class BrowserHoldStartTool:
    name = "browser_hold_start"
    description = (
        "Automatically press and hold a human-verification control on a fresh masked visual "
        "snapshot using its exact candidate ref and snapshot_id. Ricky resolves the control's "
        "center locally; do not supply coordinates. Use this for press-and-hold challenges "
        "instead of asking the user. The "
        "runtime keeps the pointer stationary and enforces a hard deadline. Inspect fresh "
        "visual feedback while holding, then use browser_hold_release. Semantic page content "
        "alone cannot establish that a visually identified overlay cleared. No dragging, "
        "purchases, protected fields, passkeys, SSO, or ordinary form submission. A new attempt "
        "requires confirmed release, a fresh challenge observation, and remaining budget."
    )
    Params = HoldStartParams
    Result = BrowserHoldStatus
    risk: ClassVar[Literal["mutating"]] = "mutating"
    capability_id = "builtin.browser.verify"
    effect_kind: ClassVar[Literal["external"]] = "external"
    unattended: ClassVar[Literal["allowed"]] = "allowed"
    review_mode: ClassVar[Literal["policy"]] = "policy"
    state_guard_id = None
    contract_version = 1

    def __init__(self, service: BrowserService) -> None:
        self._service = service

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        target = self.Params.model_validate(args).target
        context = self._service.coordinate_context(self._service.holds.coordinate_target(target))
        return make_effect_identity(
            operation=self.name,
            target=context.origin or "opaque-origin",
            occurrence=f"{target.session_id}/{target.page_id}/{target.snapshot_id}/{target.ref}",
            summary="Start one bounded human-verification hold",
        )

    def bind_effect_action(self, action_id: str, action_key: str) -> None:
        self._service.bind_effect_action(action_id, action_key)

    async def settle_effect_action(self, action_id: str) -> None:
        await self._service.settle_effect_action(action_id)

    async def run(self, params: HoldStartParams, ctx: ToolContext) -> ToolResult:
        from ricky.browser.tools import _browser_action_error

        try:
            status = await self._service.holds.start(params.target, provider=ctx.session.provider)
        except BrowserError as exc:
            return _browser_action_error(exc)
        return _result(status, effect=True)


class BrowserHoldStatusTool:
    name = "browser_hold_status"
    description = "Read the runtime-owned state and remaining deadline of one verification hold."
    Params = HoldParams
    Result = BrowserHoldStatus
    risk: ClassVar[Literal["read_only"]] = "read_only"
    capability_id = "builtin.browser.verify"
    effect_kind: ClassVar[Literal["none"]] = "none"
    unattended: ClassVar[Literal["allowed"]] = "allowed"
    review_mode: ClassVar[Literal["policy"]] = "policy"
    state_guard_id = None
    contract_version = 1

    def __init__(self, service: BrowserService) -> None:
        self._service = service

    async def run(self, params: HoldParams, ctx: ToolContext) -> ToolResult:
        from ricky.browser.tools import _browser_reference_result

        del ctx
        try:
            return _result(self._service.holds.status(params.hold_id))
        except BrowserError as exc:
            return _browser_reference_result(exc)


class BrowserHoldReleaseTool:
    name = "browser_hold_release"
    description = (
        "Release one owned verification hold and wait for input cleanup. Safe to call after "
        "automatic deadline release; never starts or repeats input. Then call "
        "browser_visual_snapshot: deadline expiry is not proof of rejection, and semantic "
        "homepage content does not prove clearance. "
        "Verify clearance and task completion separately."
    )
    Params = HoldParams
    Result = BrowserHoldStatus
    risk: ClassVar[Literal["mutating"]] = "mutating"
    capability_id = "builtin.browser.verify"
    effect_kind: ClassVar[Literal["external"]] = "external"
    unattended: ClassVar[Literal["allowed"]] = "allowed"
    review_mode: ClassVar[Literal["policy"]] = "policy"
    state_guard_id = None
    contract_version = 1

    def __init__(self, service: BrowserService) -> None:
        self._service = service

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        hold_id = self.Params.model_validate(args).hold_id
        status = self._service.holds.status(hold_id)
        return make_effect_identity(
            operation=self.name,
            target=status.session_id,
            occurrence=hold_id,
            summary="Release one owned verification hold",
        )

    async def run(self, params: HoldParams, ctx: ToolContext) -> ToolResult:
        from ricky.browser.tools import _browser_action_error

        del ctx
        try:
            return _result(await self._service.holds.release(params.hold_id), effect=True)
        except BrowserError as exc:
            return _browser_action_error(exc)
