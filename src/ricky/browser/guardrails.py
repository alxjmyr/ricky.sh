"""Capability-owned browser guardrails and deterministic scope compilation."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, ClassVar, Literal, cast

from pydantic import Field, JsonValue, field_validator, model_validator

from ricky.browser.types import BrowserModel
from ricky.capabilities.guardrails import (
    AuthenticatedSource,
    CollectedGuardrailField,
    CompiledGuardrail,
    GuardrailDecision,
    GuardrailEvaluator,
    GuardrailFieldDecision,
    GuardrailFieldProposal,
    GuardrailIntakeField,
    GuardrailIntakeSpec,
    GuardrailUsage,
    GuardrailVerdict,
    compile_guardrail,
)
from ricky.executions.browser import (
    BrowserAttachmentPin,
    BrowserBudgetOperation,
    BrowserExecutionBudget,
    BrowserExecutionMode,
    BrowserExecutionScope,
    BrowserProtectedResourcePin,
    BrowserResourcePin,
)
from ricky.profiles import ProfileResourceRef

BrowserGuardrailCapability = Literal[
    "builtin.browser.read",
    "builtin.browser.interact",
    "builtin.protected_value.use",
    "builtin.browser.commit",
]

BROWSER_READ_TOOLS = frozenset(
    {
        "browser_resources",
        "browser_session_open",
        "browser_session_close",
        "browser_pages",
        "browser_page_select",
        "browser_navigate",
        "browser_scroll",
        "browser_snapshot",
        "browser_visual_snapshot",
    }
)
BROWSER_INTERACT_TOOLS = frozenset(
    {
        "browser_session_open_resource",
        "browser_click",
        "browser_fill",
        "browser_select",
        "browser_set_checked",
        "browser_press_key",
        "browser_upload",
        "browser_download",
        "browser_coordinate_click",
    }
)
BROWSER_PROTECTED_TOOLS = frozenset({"browser_fill_protected"})
BROWSER_COMMIT_TOOLS = frozenset({"browser_commit", "browser_coordinate_commit"})
BROWSER_TOOLS_BY_CAPABILITY: dict[BrowserGuardrailCapability, frozenset[str]] = {
    "builtin.browser.read": BROWSER_READ_TOOLS,
    "builtin.browser.interact": BROWSER_INTERACT_TOOLS,
    "builtin.protected_value.use": BROWSER_PROTECTED_TOOLS,
    "builtin.browser.commit": BROWSER_COMMIT_TOOLS,
}


class BrowserProtectedSelection(BrowserModel):
    """Owner-sourced protected alias and fields before local revision resolution."""

    resource: ProfileResourceRef
    fields: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("fields")
    @classmethod
    def _canonical_fields(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(values))
        if len(canonical) != len(set(canonical)) or any(
            not value or len(value) > 64 for value in canonical
        ):
            raise ValueError("protected fields must be unique bounded names")
        return canonical


class BrowserAuthenticatedOriginSelection(BrowserModel):
    """Reviewed origin ceiling for one authenticated browser resource."""

    resource: ProfileResourceRef
    origins: tuple[str, ...] = Field(min_length=1, max_length=100)

    @field_validator("origins")
    @classmethod
    def _canonical_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        validated = BrowserResourcePin(
            resource=ProfileResourceRef(profile="default", name="validation"),
            kind="persistent",
            configuration_digest="0" * 64,
            authenticated_origin_ceiling=values,
        )
        return validated.authenticated_origin_ceiling


class BrowserGuardrailConstraints(BrowserModel):
    """Authenticated owner ceiling shared by contract and authority compilation."""

    version: Literal[1] = 1
    capability_id: BrowserGuardrailCapability
    mode: BrowserExecutionMode
    allowed_tools: tuple[str, ...] = Field(min_length=1, max_length=30)
    resources: tuple[ProfileResourceRef, ...] = Field(default=(), max_length=50)
    authenticated_origins: tuple[BrowserAuthenticatedOriginSelection, ...] = Field(
        default=(), max_length=50
    )
    allow_ephemeral: bool = False
    allow_public_https_research: bool = False
    allow_masked_visual_observations: bool = False
    private_origin_ceiling: tuple[str, ...] = Field(default=(), max_length=100)
    attachment_ids: tuple[str, ...] = Field(default=(), max_length=100)
    protected_values: tuple[BrowserProtectedSelection, ...] = Field(default=(), max_length=100)

    @field_validator("allowed_tools", "private_origin_ceiling", "attachment_ids")
    @classmethod
    def _canonical_strings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(values))
        if len(canonical) != len(set(canonical)) or any(
            value != value.strip() or not value or len(value) > 500 for value in canonical
        ):
            raise ValueError("browser guardrail string selections must be unique and bounded")
        return canonical

    @model_validator(mode="after")
    def _coherent_capability(self) -> BrowserGuardrailConstraints:
        ceiling = BROWSER_TOOLS_BY_CAPABILITY[self.capability_id]
        if not set(self.allowed_tools) <= ceiling:
            raise ValueError("browser guardrail selected a tool outside its capability")
        if self.mode == "read_only":
            if self.capability_id == "builtin.browser.interact" and set(self.allowed_tools) != {
                "browser_session_open_resource"
            }:
                raise ValueError(
                    "read-only interact scope may select only persistent-resource disclosure"
                )
            if self.capability_id in {
                "builtin.protected_value.use",
                "builtin.browser.commit",
            }:
                raise ValueError("protected use and commit require transaction mode")
            if self.attachment_ids or self.protected_values:
                raise ValueError("read-only browser guardrails cannot select private effects")
        if self.capability_id != "builtin.browser.read" and (
            self.allow_ephemeral
            or self.allow_public_https_research
            or self.allow_masked_visual_observations
        ):
            raise ValueError("read-session disclosures belong to the browser read guardrail")
        if self.capability_id != "builtin.browser.interact" and self.attachment_ids:
            raise ValueError("attachment selection belongs to browser interaction")
        if self.capability_id != "builtin.protected_value.use" and self.protected_values:
            raise ValueError("protected aliases belong to protected-value use")
        resource_refs = {item.qualified for item in self.resources}
        origin_refs = [item.resource.qualified for item in self.authenticated_origins]
        if len(origin_refs) != len(set(origin_refs)):
            raise ValueError("authenticated browser origin selections must be unique")
        if set(origin_refs) != resource_refs:
            raise ValueError(
                "each configured browser resource requires one authenticated origin ceiling"
            )
        return self


def browser_guardrail_constraints(guardrail: CompiledGuardrail) -> BrowserGuardrailConstraints:
    """Validate one compiled browser guardrail's exact exported constraint shape."""

    expected = _SCHEMA_TO_CAPABILITY.get(guardrail.schema_id)
    if expected is None or guardrail.schema_version != 1 or guardrail.capability_id != expected:
        raise ValueError("compiled browser guardrail has an unknown schema identity")
    constraints = BrowserGuardrailConstraints.model_validate_json(
        json.dumps(guardrail.constraints, sort_keys=True, separators=(",", ":"))
    )
    if constraints.capability_id != expected:
        raise ValueError("compiled browser guardrail capability does not match its constraints")
    return constraints


def compile_browser_execution_scope(
    *,
    mode: BrowserExecutionMode,
    guardrails: Iterable[CompiledGuardrail],
    budget: BrowserExecutionBudget,
    resource_pins: tuple[BrowserResourcePin, ...] = (),
    attachment_pins: tuple[BrowserAttachmentPin, ...] = (),
    protected_resource_pins: tuple[BrowserProtectedResourcePin, ...] = (),
) -> BrowserExecutionScope:
    """Merge reviewed guardrails with locally resolved immutable pins without widening."""

    constraints = tuple(browser_guardrail_constraints(item) for item in guardrails)
    capabilities = [item.capability_id for item in constraints]
    if len(capabilities) != len(set(capabilities)):
        raise ValueError("browser execution cannot repeat a capability guardrail")
    if any(item.mode != mode for item in constraints):
        raise ValueError("browser guardrail mode does not match the execution mode")
    allowed_tools = tuple(sorted({tool for item in constraints for tool in item.allowed_tools}))
    if not allowed_tools:
        raise ValueError("browser execution requires at least one selected tool")

    selected_resources = {resource.qualified for item in constraints for resource in item.resources}
    pinned_resources = {item.resource.qualified: item for item in resource_pins}
    if set(pinned_resources) != selected_resources:
        raise ValueError("resolved browser resource pins differ from reviewed selections")
    reviewed_resource_origins = {
        selection.resource.qualified: selection.origins
        for item in constraints
        for selection in item.authenticated_origins
    }
    if set(reviewed_resource_origins) != selected_resources or any(
        pinned_resources[qualified].authenticated_origin_ceiling != origins
        for qualified, origins in reviewed_resource_origins.items()
    ):
        raise ValueError("resolved browser origins differ from reviewed selections")

    selected_attachments = {
        attachment for item in constraints for attachment in item.attachment_ids
    }
    pinned_attachments = {item.id: item for item in attachment_pins}
    if set(pinned_attachments) != selected_attachments:
        raise ValueError("resolved attachment pins differ from reviewed selections")

    protected_selections = {
        item.resource.qualified: set(item.fields)
        for constraint in constraints
        for item in constraint.protected_values
    }
    pinned_protected = {item.resource.qualified: item for item in protected_resource_pins}
    if set(pinned_protected) != set(protected_selections) or any(
        set(pinned_protected[qualified].fields) != fields
        for qualified, fields in protected_selections.items()
    ):
        raise ValueError("resolved protected-resource pins differ from reviewed selections")

    operations = _operations_for_tools(frozenset(allowed_tools))
    private_origins = tuple(
        sorted({origin for item in constraints for origin in item.private_origin_ceiling})
    )
    return BrowserExecutionScope(
        mode=mode,
        resources=tuple(pinned_resources[key] for key in sorted(pinned_resources)),
        allow_ephemeral=any(item.allow_ephemeral for item in constraints),
        allow_public_https_research=any(item.allow_public_https_research for item in constraints),
        private_origin_ceiling=private_origins,
        allowed_tools=allowed_tools,
        allowed_operations=operations,
        allow_masked_visual_observations=any(
            item.allow_masked_visual_observations for item in constraints
        ),
        attachments=tuple(pinned_attachments[key] for key in sorted(pinned_attachments)),
        protected_resources=tuple(pinned_protected[key] for key in sorted(pinned_protected)),
        budget=budget,
    )


def _operations_for_tools(tools: frozenset[str]) -> tuple[BrowserBudgetOperation, ...]:
    operations: set[BrowserBudgetOperation] = set()
    if tools & {"browser_session_open", "browser_session_open_resource"}:
        operations.update({"session_starts", "controlled_pages"})
    if "browser_navigate" in tools:
        operations.add("navigations")
    if "browser_scroll" in tools:
        operations.add("scrolls")
    if "browser_snapshot" in tools:
        operations.add("semantic_observations")
    if "browser_visual_snapshot" in tools:
        operations.add("visual_observations")
    interaction_tools = BROWSER_INTERACT_TOOLS - {"browser_session_open_resource"}
    if tools & interaction_tools:
        operations.update({"interactions", "navigations", "created_pages", "controlled_pages"})
    if "browser_fill_protected" in tools:
        operations.update(
            {
                "protected_materializations",
                "navigations",
                "created_pages",
                "controlled_pages",
            }
        )
    if "browser_upload" in tools:
        operations.update({"uploads", "upload_bytes"})
    if "browser_download" in tools:
        operations.update({"downloads", "download_bytes"})
    if tools & BROWSER_COMMIT_TOOLS:
        operations.update(
            {
                "transaction_commits",
                "navigations",
                "created_pages",
                "controlled_pages",
                "parked_browsers",
            }
        )
    return tuple(sorted(operations))


_FIELDS = (
    GuardrailIntakeField(
        name="mode",
        value_type="string",
        description="Exact background browser mode: read_only or transaction.",
        question="Should this browser execution be read-only or transaction-capable?",
    ),
    GuardrailIntakeField(
        name="allowed_tools",
        value_type="string",
        description="Comma-separated exact browser tool names authorized by this instruction.",
        question="Which exact browser operations should this execution be allowed to use?",
    ),
    GuardrailIntakeField(
        name="resources",
        value_type="string",
        description="Comma-separated qualified configured browser resource aliases.",
        required=False,
        question="Which configured browser resource aliases may be used?",
    ),
    GuardrailIntakeField(
        name="authenticated_origins",
        value_type="string",
        description=(
            "Semicolon-separated configured-resource origin ceilings in "
            "alias#https://origin|https://origin form."
        ),
        required=False,
        question="Which exact HTTPS origins may each configured browser resource reach?",
    ),
    GuardrailIntakeField(
        name="allow_ephemeral",
        value_type="boolean",
        description="Whether an owned ephemeral headless browser may be opened.",
        required=False,
        question="May Ricky open an ephemeral headless browser?",
    ),
    GuardrailIntakeField(
        name="allow_public_https_research",
        value_type="boolean",
        description="Whether public HTTPS research navigation is allowed.",
        required=False,
        question="May Ricky research public HTTPS sites?",
    ),
    GuardrailIntakeField(
        name="allow_masked_visual_observations",
        value_type="boolean",
        description="Whether masked screenshots may reach the pinned provider.",
        required=False,
        question="May masked browser screenshots be sent to the configured model provider?",
    ),
    GuardrailIntakeField(
        name="private_origin_ceiling",
        value_type="string",
        description="Comma-separated exact HTTPS private origins, normally empty.",
        required=False,
        question="Which exact private HTTPS origins, if any, may be reached?",
    ),
    GuardrailIntakeField(
        name="attachment_ids",
        value_type="string",
        description="Comma-separated execution attachment identifiers allowed for upload.",
        required=False,
        question="Which exact attachments may be uploaded?",
    ),
    GuardrailIntakeField(
        name="protected_values",
        value_type="string",
        description=("Semicolon-separated protected selections in alias#field|field form."),
        required=False,
        question="Which protected aliases and fields may this execution use?",
    ),
)

_SCHEMA_TO_CAPABILITY: dict[str, BrowserGuardrailCapability] = {
    "browser.read": "builtin.browser.read",
    "browser.interact": "builtin.browser.interact",
    "protected_value.use": "builtin.protected_value.use",
    "browser.commit": "builtin.browser.commit",
}


class _BrowserGuardrailEvaluator:
    schema_version: ClassVar[int] = 1
    capability_id: ClassVar[BrowserGuardrailCapability]
    schema_id: ClassVar[str]
    tools: ClassVar[frozenset[str]]
    intake_spec: ClassVar[GuardrailIntakeSpec]

    def normalize_field(self, proposal: GuardrailFieldProposal) -> GuardrailFieldDecision:
        field = self.intake_spec.get(proposal.field)
        if field is None:
            return GuardrailFieldDecision(
                accepted=False,
                reason=f"unknown browser guardrail field: {proposal.field}",
            )
        try:
            value = _normalize_field(proposal.field, proposal.value)
        except (TypeError, ValueError) as exc:
            return GuardrailFieldDecision(
                accepted=False,
                question=f"{field.question} The supplied value was invalid: {exc}"[:500],
            )
        return GuardrailFieldDecision(accepted=True, value=cast(JsonValue, value))

    def validate_collected(
        self,
        fields: tuple[CollectedGuardrailField, ...],
        sources: tuple[AuthenticatedSource, ...],
    ) -> GuardrailDecision:
        source_by_id = {source.message_id: source for source in sources}
        raw: dict[str, Any] = {}
        cited: list[str] = []
        for item in fields:
            if (
                item.capability_id != self.capability_id
                or item.schema_id != self.schema_id
                or item.schema_version != self.schema_version
            ):
                return GuardrailDecision(reason="browser guardrail field has another identity")
            source = source_by_id.get(item.source_message_id)
            if source is None or source.text_digest != item.source_text_digest:
                return GuardrailDecision(
                    reason="browser guardrail field lacks authenticated provenance"
                )
            raw[item.field] = item.value
            if item.source_message_id not in cited:
                cited.append(item.source_message_id)
        missing = tuple(
            field.question
            for field in self.intake_spec.fields
            if field.required and raw.get(field.name) in {None, ""}
        )
        if missing:
            return GuardrailDecision(questions=missing)
        try:
            constraints = _constraints_from_raw(self.capability_id, raw)
        except ValueError as exc:
            return GuardrailDecision(
                questions=(f"Those browser boundaries are not usable: {exc}"[:500],)
            )
        summary = _summary(constraints)
        return GuardrailDecision(
            guardrail=compile_guardrail(
                capability_id=self.capability_id,
                schema_id=self.schema_id,
                schema_version=1,
                constraints=constraints.model_dump(mode="json"),
                sources=tuple(source_by_id[source_id] for source_id in cited),
                summary=summary,
            )
        )

    def summarize(self, guardrail: CompiledGuardrail) -> str:
        return _summary(browser_guardrail_constraints(guardrail))

    def evaluate_call(
        self,
        guardrail: CompiledGuardrail,
        tool_name: str,
        args: dict[str, object],
        usage: GuardrailUsage,
    ) -> GuardrailVerdict:
        del args, usage
        constraints = browser_guardrail_constraints(guardrail)
        allowed = tool_name in constraints.allowed_tools and tool_name in self.tools
        return GuardrailVerdict(
            allowed=allowed,
            reason=(
                "browser call is inside the authenticated tool ceiling"
                if allowed
                else "browser tool is outside the authenticated tool ceiling"
            ),
            usage_delta={"calls": 1} if allowed else {},
        )


def _evaluator(
    name: str,
    *,
    capability_id: BrowserGuardrailCapability,
    schema_id: str,
) -> type[_BrowserGuardrailEvaluator]:
    return type(
        name,
        (_BrowserGuardrailEvaluator,),
        {
            "capability_id": capability_id,
            "schema_id": schema_id,
            "tools": BROWSER_TOOLS_BY_CAPABILITY[capability_id],
            "intake_spec": GuardrailIntakeSpec(
                schema_id=schema_id,
                schema_version=1,
                fields=_FIELDS,
            ),
        },
    )


BrowserReadGuardrailEvaluator = _evaluator(
    "BrowserReadGuardrailEvaluator",
    capability_id="builtin.browser.read",
    schema_id="browser.read",
)
BrowserInteractGuardrailEvaluator = _evaluator(
    "BrowserInteractGuardrailEvaluator",
    capability_id="builtin.browser.interact",
    schema_id="browser.interact",
)
ProtectedValueUseGuardrailEvaluator = _evaluator(
    "ProtectedValueUseGuardrailEvaluator",
    capability_id="builtin.protected_value.use",
    schema_id="protected_value.use",
)
BrowserCommitGuardrailEvaluator = _evaluator(
    "BrowserCommitGuardrailEvaluator",
    capability_id="builtin.browser.commit",
    schema_id="browser.commit",
)


def browser_guardrail_evaluators() -> tuple[GuardrailEvaluator, ...]:
    """Return the four production browser capability guardrail evaluators."""

    return cast(
        tuple[GuardrailEvaluator, ...],
        (
            BrowserReadGuardrailEvaluator(),
            BrowserInteractGuardrailEvaluator(),
            ProtectedValueUseGuardrailEvaluator(),
            BrowserCommitGuardrailEvaluator(),
        ),
    )


def _normalize_field(name: str, value: JsonValue) -> JsonValue:
    if name == "mode":
        if value not in {"read_only", "transaction"}:
            raise ValueError("mode must be read_only or transaction")
        return value
    if name in {
        "allow_ephemeral",
        "allow_public_https_research",
        "allow_masked_visual_observations",
    }:
        if not isinstance(value, bool):
            raise TypeError("expected a boolean")
        return value
    if not isinstance(value, str):
        raise TypeError("expected bounded comma-separated text")
    value = value.strip()
    if not value or len(value) > 10_000:
        raise ValueError("selection text is blank or too long")
    return value


def _constraints_from_raw(
    capability_id: BrowserGuardrailCapability,
    raw: dict[str, Any],
) -> BrowserGuardrailConstraints:
    authenticated_origins: list[BrowserAuthenticatedOriginSelection] = []
    for selection in _split(str(raw.get("authenticated_origins", "")), separator=";"):
        alias, marker, origins = selection.partition("#")
        if not marker:
            raise ValueError("authenticated origin selection must use alias#origin|origin")
        authenticated_origins.append(
            BrowserAuthenticatedOriginSelection(
                resource=ProfileResourceRef.from_qualified(alias),
                origins=tuple(item for item in origins.split("|") if item),
            )
        )
    protected: list[BrowserProtectedSelection] = []
    for selection in _split(str(raw.get("protected_values", "")), separator=";"):
        alias, marker, fields = selection.partition("#")
        if not marker:
            raise ValueError("protected selection must use alias#field|field")
        protected.append(
            BrowserProtectedSelection(
                resource=ProfileResourceRef.from_qualified(alias),
                fields=tuple(item for item in fields.split("|") if item),
            )
        )
    return BrowserGuardrailConstraints(
        capability_id=capability_id,
        mode=cast(BrowserExecutionMode, raw["mode"]),
        allowed_tools=_split(str(raw["allowed_tools"])),
        resources=tuple(
            ProfileResourceRef.from_qualified(item)
            for item in _split(str(raw.get("resources", "")))
        ),
        authenticated_origins=tuple(authenticated_origins),
        allow_ephemeral=bool(raw.get("allow_ephemeral", False)),
        allow_public_https_research=bool(raw.get("allow_public_https_research", False)),
        allow_masked_visual_observations=bool(raw.get("allow_masked_visual_observations", False)),
        private_origin_ceiling=_split(str(raw.get("private_origin_ceiling", ""))),
        attachment_ids=_split(str(raw.get("attachment_ids", ""))),
        protected_values=tuple(protected),
    )


def _split(value: str, *, separator: str = ",") -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(separator) if item.strip())


def _summary(constraints: BrowserGuardrailConstraints) -> str:
    resources = ", ".join(item.qualified for item in constraints.resources) or "none"
    authenticated = (
        "; ".join(
            f"{item.resource.qualified}#{'|'.join(item.origins)}"
            for item in constraints.authenticated_origins
        )
        or "none"
    )
    return (
        f"{constraints.mode} {constraints.capability_id}: "
        f"{', '.join(constraints.allowed_tools)}; resources: {resources}; "
        f"authenticated origins: {authenticated}; "
        f"masked visuals: {'allowed' if constraints.allow_masked_visual_observations else 'off'}"
    )
