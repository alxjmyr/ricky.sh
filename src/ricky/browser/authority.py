"""Provider-neutral delegated authority evaluators for browser effects."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import ClassVar, cast

from pydantic import TypeAdapter, ValidationError

from ricky.authority.registry import AuthorityEvaluator
from ricky.authority.types import AuthorityScope, AuthorityVerdict
from ricky.browser.guardrails import (
    BROWSER_COMMIT_TOOLS,
    BROWSER_INTERACT_TOOLS,
    BROWSER_PROTECTED_TOOLS,
    BrowserGuardrailConstraints,
)
from ricky.browser.tools import (
    BrowserClickTool,
    BrowserCommitParams,
    BrowserCoordinateClickParams,
    BrowserCoordinateCommitParams,
    BrowserDownloadParams,
    BrowserFillParams,
    BrowserPressKeyParams,
    BrowserProtectedFillParams,
    BrowserSelectParams,
    BrowserSetCheckedParams,
    BrowserUploadParams,
)
from ricky.browser.types import BrowserCommitEnvelope, BrowserFinancialTransactionEnvelope
from ricky.tools.base import (
    EffectIdentity,
    EffectReceipt,
    ToolResult,
    make_effect_identity,
)

_ENVELOPE = TypeAdapter(BrowserCommitEnvelope)
_AUTHORITY_TO_GROUP = {
    "browser_interact": "builtin.browser.interact",
    "protected_value_use": "builtin.protected_value.use",
    "browser_commit": "builtin.browser.commit",
}
_SCHEMA = {
    "browser_interact": "browser.interact",
    "protected_value_use": "protected_value.use",
    "browser_commit": "browser.commit",
}
_PARAMS = {
    "browser_click": BrowserClickTool.Params,
    "browser_fill": BrowserFillParams,
    "browser_select": BrowserSelectParams,
    "browser_set_checked": BrowserSetCheckedParams,
    "browser_press_key": BrowserPressKeyParams,
    "browser_upload": BrowserUploadParams,
    "browser_download": BrowserDownloadParams,
    "browser_coordinate_click": BrowserCoordinateClickParams,
    "browser_fill_protected": BrowserProtectedFillParams,
    "browser_commit": BrowserCommitParams,
    "browser_coordinate_commit": BrowserCoordinateCommitParams,
}


class _BrowserAuthorityEvaluator:
    capability: ClassVar[str]
    schema_id: ClassVar[str]
    schema_version: ClassVar[int] = 1
    tools: ClassVar[frozenset[str]]
    scope_only_tools: ClassVar[frozenset[str]] = frozenset()
    uses_owner_financial_ceiling: ClassVar[bool] = False

    def summarize(self, scope: AuthorityScope) -> str:
        constraints = self._scope(scope)
        return f"{constraints.mode} {self.capability}: {', '.join(constraints.allowed_tools)}"

    def evaluate_call(
        self,
        scope: AuthorityScope,
        tool_name: str,
        args: dict[str, object],
    ) -> AuthorityVerdict:
        constraints = self._scope(scope)
        if tool_name not in self.tools or tool_name not in constraints.allowed_tools:
            return AuthorityVerdict(
                allowed=False,
                reason="browser tool is outside the delegated authority scope",
            )
        params_type = _PARAMS[tool_name]
        try:
            parsed = params_type.model_validate(args)
        except ValidationError as exc:
            return AuthorityVerdict(
                allowed=False,
                reason=f"invalid canonical browser call: {exc}"[:1_000],
            )
        if tool_name == "browser_upload":
            upload = BrowserUploadParams.model_validate(parsed)
            if upload.attachments or not set(upload.execution_attachment_ids) <= set(
                constraints.attachment_ids
            ):
                return AuthorityVerdict(
                    allowed=False,
                    reason="browser upload source is outside approved execution attachments",
                )
        if tool_name == "browser_fill_protected":
            protected = BrowserProtectedFillParams.model_validate(parsed)
            fields = {
                item.resource.qualified: set(item.fields) for item in constraints.protected_values
            }
            if protected.field not in fields.get(protected.protected_value, set()):
                return AuthorityVerdict(
                    allowed=False,
                    reason="protected alias or field is outside delegated authority",
                )
        amount_minor = 0
        currency: str | None = None
        if tool_name in BROWSER_COMMIT_TOOLS:
            payload = parsed.model_dump(mode="python").get("envelope")
            envelope = _ENVELOPE.validate_python(payload)
            if isinstance(envelope, BrowserFinancialTransactionEnvelope):
                try:
                    amount_minor = browser_money_minor_units(
                        envelope.total.amount, envelope.total.currency
                    )
                except ValueError as exc:
                    return AuthorityVerdict(allowed=False, reason=str(exc))
                currency = envelope.total.currency if amount_minor > 0 else None
        return AuthorityVerdict(
            allowed=True,
            reason="browser call is within delegated authority",
            amount_minor=amount_minor,
            currency=currency,
        )

    def effect_identity(
        self,
        scope: AuthorityScope,
        tool_name: str,
        args: dict[str, object],
    ) -> EffectIdentity:
        self._scope(scope)
        params = _PARAMS[tool_name].model_validate(args)
        canonical = params.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        target = canonical.get("target")
        target_label = "browser-target"
        if isinstance(target, dict):
            target_label = ":".join(
                str(target.get(name, "")) for name in ("session_id", "page_id", "ref")
            )[:500]
        return make_effect_identity(
            operation=tool_name,
            target=target_label,
            occurrence=digest,
            summary=f"Delegated browser effect: {tool_name}",
        )

    def receipt(self, scope: AuthorityScope, result: ToolResult) -> EffectReceipt:
        self._scope(scope)
        if result.effect_receipt is not None:
            return result.effect_receipt
        return EffectReceipt(disposition="not_performed" if result.is_error else "in_doubt")

    def consumes_grant(self, scope: AuthorityScope, receipt: EffectReceipt) -> bool:
        self._scope(scope)
        return self.capability == "browser_commit" and receipt.disposition in {
            "performed",
            "in_doubt",
        }

    def _scope(self, scope: AuthorityScope) -> BrowserGuardrailConstraints:
        if (
            scope.capability != self.capability
            or scope.schema_id != self.schema_id
            or scope.schema_version != self.schema_version
        ):
            raise ValueError("browser authority scope identity mismatch")
        constraints = BrowserGuardrailConstraints.model_validate(scope.constraints)
        if constraints.capability_id != _AUTHORITY_TO_GROUP[self.capability]:
            raise ValueError("browser authority constraints have another capability")
        return constraints


class BrowserInteractAuthorityEvaluator(_BrowserAuthorityEvaluator):
    capability: ClassVar[str] = "browser_interact"
    schema_id: ClassVar[str] = _SCHEMA[capability]
    tools: ClassVar[frozenset[str]] = BROWSER_INTERACT_TOOLS - {"browser_session_open_resource"}
    scope_only_tools: ClassVar[frozenset[str]] = frozenset({"browser_session_open_resource"})


class ProtectedValueUseAuthorityEvaluator(_BrowserAuthorityEvaluator):
    capability: ClassVar[str] = "protected_value_use"
    schema_id: ClassVar[str] = _SCHEMA[capability]
    tools: ClassVar[frozenset[str]] = BROWSER_PROTECTED_TOOLS


class BrowserCommitAuthorityEvaluator(_BrowserAuthorityEvaluator):
    capability: ClassVar[str] = "browser_commit"
    schema_id: ClassVar[str] = _SCHEMA[capability]
    tools: ClassVar[frozenset[str]] = BROWSER_COMMIT_TOOLS
    # The exact amount remains subject to a fresh transaction approval. The
    # durable grant carries the installation owner's spend ceiling so the
    # later approved occurrence can reserve its exact amount.
    uses_owner_financial_ceiling: ClassVar[bool] = True


def browser_authority_evaluators() -> tuple[AuthorityEvaluator, ...]:
    """Return every production evaluator for unattended browser external effects."""

    return cast(
        tuple[AuthorityEvaluator, ...],
        (
            BrowserInteractAuthorityEvaluator(),
            ProtectedValueUseAuthorityEvaluator(),
            BrowserCommitAuthorityEvaluator(),
        ),
    )


_ZERO_DECIMAL = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)
_THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


def browser_money_minor_units(amount: str, currency: str) -> int:
    """Convert a canonical browser amount to bounded ISO-style minor units."""

    exponent = 0 if currency in _ZERO_DECIMAL else 3 if currency in _THREE_DECIMAL else 2
    scaled = Decimal(amount) * (10**exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError("financial amount has unsupported fractional minor units")
    value = int(scaled)
    if value > 100_000_000:
        raise ValueError("financial amount exceeds delegated-authority bounds")
    return value
