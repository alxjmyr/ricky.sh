from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from authority_support import install_sandbox_runtime
from ricky.agent.session import AgentSession
from ricky.capabilities import (
    AuthenticatedSource,
    CapabilityRegistryError,
    CapabilitySpec,
    CollectedGuardrailField,
    GuardrailFieldProposal,
    GuardrailRegistry,
    build_capability_registry,
    derive_skill_owners,
    resolve_capability_policy,
    tool_contract_digest,
    validate_capability_inventory,
    validate_capability_policy,
    validate_foreground_live_policy,
    validate_tool_contract,
)
from ricky.config import AgentCapabilityPolicySettings, RickySettings
from ricky.runtime.composition import build_capability_runtime
from ricky.skills.registry import SkillRegistry, discover_skills
from ricky.tools import EffectIdentity, StateGuardRegistry, ToolContractError, ToolRegistry
from ricky.tools.base import Risk, ToolContext, ToolResult
from sandbox_support import SandboxGuardrailEvaluator, SandboxReservationTool

APPROVED_BUILTIN_TOOL_MAP = {
    "builtin.project.read": {"read_file", "list_dir", "glob_search", "grep_search"},
    "builtin.project.mutate": {"write_file", "edit_file"},
    "builtin.host.execute": {"run_shell"},
    "builtin.session.read": {"read_tool_artifact"},
    "builtin.session.mutate": {"update_tasks"},
    "builtin.memory.read": {"recall"},
    "builtin.memory.mutate": {"remember", "forget"},
    "builtin.skill.use": {"use_skill", "read_skill_resource"},
    "builtin.automation.read": {
        "validate_workflow",
        "read_execution_request",
        "list_execution_requests",
        "list_delegations",
    },
    "builtin.automation.mutate": {
        "start_workflow",
        "start_named_job",
        "delegate_task",
        "cancel_execution_request",
        "revoke_delegation",
    },
    "builtin.task.read": {
        "search_durable_tasks",
        "read_durable_task",
        "list_task_artifacts",
        "read_task_artifact",
    },
    "builtin.task.mutate": {
        "create_durable_task",
        "park_for_review",
        "claim_durable_task",
        "renew_durable_task_lease",
        "update_durable_task_progress",
        "update_durable_task_tags",
        "wait_durable_task",
        "block_durable_task",
        "complete_durable_task",
        "release_durable_task",
        "cancel_durable_task",
        "reopen_durable_task",
        "write_task_artifact",
        "edit_task_artifact",
    },
    "builtin.notification.mutate": {"notify_user"},
    "builtin.browser.read": {
        "browser_resources",
        "browser_session_open",
        "browser_session_close",
        "browser_pages",
        "browser_page_select",
        "browser_navigate",
        "browser_scroll",
        "browser_snapshot",
        "browser_visual_snapshot",
    },
    "builtin.browser.interact": {
        "browser_session_open_resource",
        "browser_click",
        "browser_fill",
        "browser_select",
        "browser_set_checked",
        "browser_press_key",
        "browser_upload",
        "browser_download",
        "browser_coordinate_click",
    },
    "builtin.browser.handoff": {"browser_handoff"},
    "builtin.browser.commit": {"browser_commit", "browser_coordinate_commit"},
    "builtin.protected_value.read": {"protected_values_catalog"},
    "builtin.protected_value.use": {"browser_fill_protected"},
    "builtin.web.read": {"web_search"},
    "builtin.email.read": {
        "gmail_search",
        "gmail_read_message",
        "gmail_read_thread",
        "gmail_list_labels",
        "gmail_list_drafts",
    },
    "builtin.email.mutate": {
        "gmail_create_draft",
        "gmail_send_message",
        "gmail_create_label",
        "gmail_modify_labels",
        "gmail_trash",
        "gmail_download_attachment",
    },
    "builtin.calendar.read": {
        "gcal_list_calendars",
        "gcal_list_events",
        "gcal_get_event",
        "gcal_check_availability",
    },
    "builtin.calendar.mutate": {
        "gcal_create_event",
        "gcal_update_event",
        "gcal_respond_to_event",
        "gcal_delete_event",
    },
    "builtin.chat.read": {
        "slack_list_channels",
        "slack_list_unread",
        "slack_find_user",
        "slack_search",
        "slack_read_messages",
        "slack_read_thread",
    },
    "builtin.chat.mutate": {
        "slack_mark_read",
        "slack_send_message",
        "slack_download_file",
    },
    "builtin.authorization.review": {"prepare_capability_use"},
}

_SHIPPED_TOOL_MODULES = (
    "ricky.tools.builtin.files",
    "ricky.tools.builtin.shell",
    "ricky.tools.builtin.tasks",
    "ricky.tools.builtin.artifacts",
    "ricky.memory.tools",
    "ricky.skills.tool",
    "ricky.workflows.tool",
    "ricky.durable_tasks.tools",
    "ricky.executions.tools",
    "ricky.notifications.tools",
    "ricky.authority.tools",
    "ricky.gateway.tools",
    "ricky.gateway.capability_use",
    "ricky.jobs.tools",
    "ricky.browser.tools",
    "ricky.protected_values.tools",
    "ricky.tools.integrations.gmail.tools",
    "ricky.tools.integrations.gcal.tools",
    "ricky.tools.integrations.slack.tools",
    "ricky.tools.integrations.web_search.tools",
)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str = "ok"


class _Tool:
    name: ClassVar[str] = "personal_lookup"
    description: ClassVar[str] = "Look up user-owned data."
    Params: ClassVar[type[BaseModel]] = _Params
    risk: ClassVar[Risk] = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        return ToolResult(content="ok")


class _GroupedTool(_Tool):
    name: ClassVar[str] = "grouped_read"
    capability_id = "builtin.sample.read"


class _ExternalTool(_Tool):
    name: ClassVar[str] = "external_write"
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.sample.mutate"
    effect_kind = "external"

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del args, ctx
        return EffectIdentity(
            operation=self.name,
            target="test",
            occurrence="one",
            summary="Perform one test effect",
            action_key="a" * 64,
        )


class _StateGuard:
    def __init__(self, guard_id: str = "test.guard") -> None:
        self.id = guard_id

    def wrap(self, tool):
        return tool


def _sample_spec(suffix: str = "read") -> CapabilitySpec:
    return CapabilitySpec(
        id=f"builtin.sample.{suffix}",
        owner="builtin",
        description="Sample tools.",
    )


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "authority": {
                "enabled": True,
                "capabilities": {"sandbox_reservation": {"enabled": True}},
            },
        }
    )


def _collected_reservation_fields(
    evaluator: SandboxGuardrailEvaluator,
    constraints: dict[str, JsonValue],
    source: AuthenticatedSource,
    *,
    quote: str,
) -> tuple[CollectedGuardrailField, ...]:
    collected = []
    for name, value in constraints.items():
        proposal = GuardrailFieldProposal(
            field=name,
            value=value,
            source_quote=source.text_snapshot,
        )
        decision = evaluator.normalize_field(proposal)
        assert decision.accepted
        collected.append(
            CollectedGuardrailField(
                capability_id=evaluator.capability_id,
                schema_id=evaluator.schema_id,
                schema_version=evaluator.schema_version,
                field=name,
                value=decision.value,
                source_message_id=source.message_id,
                source_text_digest=source.text_digest,
                source_quote=quote,
            )
        )
    return tuple(collected)


@pytest.mark.asyncio
async def test_composed_builtin_tools_have_exactly_one_primary_capability(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    async with build_capability_runtime(
        settings, session=session, project_root=tmp_path
    ) as runtime:
        for tool in runtime.tools:
            definition = runtime.capability_registry.for_resource("tool", tool.name)
            assert definition is not None, tool.name
            assert [
                resource.id
                for resource in definition.resources
                if resource.kind == "tool" and resource.id == tool.name
            ] == [tool.name]


def test_shipped_tool_declarations_match_the_approved_provider_neutral_map() -> None:
    actual: dict[str, set[str]] = {}
    guards = StateGuardRegistry([_StateGuard("durable_task.lease")])
    for module_name in _SHIPPED_TOOL_MODULES:
        module = importlib.import_module(module_name)
        for candidate in vars(module).values():
            if not inspect.isclass(candidate) or candidate.__module__ != module_name:
                continue
            if candidate.__name__.startswith("_") or not hasattr(candidate, "Params"):
                continue
            declared = cast(Any, candidate)
            validate_tool_contract(declared, state_guards=guards)
            capability_id = getattr(declared, "capability_id", None)
            expected_unattended = "forbidden" if declared.name == "browser_handoff" else "allowed"
            assert declared.unattended == expected_unattended
            if capability_id is not None:
                actual.setdefault(capability_id, set()).add(declared.name)

    assert actual == APPROVED_BUILTIN_TOOL_MAP


def test_external_tool_derives_one_direct_namespaced_capability() -> None:
    registry = build_capability_registry(
        [_Tool()],
        SkillRegistry(),
        capability_specs=(),
        external_tool_owners={"personal_lookup": "user"},
    )
    definition = registry.require("user.personal_lookup")
    assert definition.kind == "direct_tool"
    assert [resource.id for resource in definition.resources] == ["personal_lookup"]


def test_external_tool_cannot_claim_builtin_namespace() -> None:
    with pytest.raises(CapabilityRegistryError, match="builtin namespace"):
        build_capability_registry(
            [_Tool()],
            SkillRegistry(),
            capability_specs=(),
            external_tool_owners={"personal_lookup": "builtin"},
        )


def test_duplicate_registered_tool_name_fails_before_mapping() -> None:
    with pytest.raises(CapabilityRegistryError, match="tool names must be unique"):
        build_capability_registry(
            [_GroupedTool(), _GroupedTool()],
            SkillRegistry(),
            capability_specs=(_sample_spec(),),
        )


@pytest.mark.parametrize("risk", ["read_only", "mutating", "destructive"])
def test_tool_contract_accepts_each_risk_with_a_coherent_effect(risk: Risk) -> None:
    class Tool(_GroupedTool):
        pass

    Tool.risk = risk
    Tool.effect_kind = "none" if risk == "read_only" else "ricky_state"

    assert validate_tool_contract(Tool())["risk"] == risk


@pytest.mark.parametrize("unattended", ["allowed", "forbidden"])
def test_tool_contract_accepts_each_unattended_value(unattended: str) -> None:
    class Tool(_GroupedTool):
        pass

    Tool.unattended = unattended

    assert validate_tool_contract(Tool())["unattended"] == unattended


@pytest.mark.parametrize("review_mode", ["policy", "fresh"])
def test_tool_contract_accepts_each_review_mode(review_mode: str) -> None:
    class Tool(_GroupedTool):
        pass

    cast(Any, Tool).review_mode = review_mode

    assert validate_tool_contract(Tool())["review_mode"] == review_mode


def test_tool_contract_defaults_review_mode_and_rejects_invalid_values() -> None:
    class Invalid(_GroupedTool):
        review_mode = "remembered"

    assert validate_tool_contract(_GroupedTool())["review_mode"] == "policy"
    with pytest.raises(CapabilityRegistryError, match="invalid review mode"):
        validate_tool_contract(Invalid())
    with pytest.raises(ToolContractError, match="invalid review mode"):
        ToolRegistry([Invalid()])


@pytest.mark.parametrize(
    ("effect_kind", "tool_type"),
    [
        ("none", _GroupedTool),
        (
            "ricky_state",
            type(
                "RickyStateTool",
                (_GroupedTool,),
                {"risk": "mutating", "effect_kind": "ricky_state"},
            ),
        ),
        ("external", _ExternalTool),
    ],
)
def test_tool_contract_accepts_each_effect_kind(effect_kind: str, tool_type: type) -> None:
    tool = tool_type()
    assert validate_tool_contract(tool)["effect_kind"] == effect_kind


def test_tool_contract_rejects_a_missing_state_guard_declaration() -> None:
    class MissingStateGuard:
        name = "missing_state_guard"
        risk = "read_only"
        capability_id = "builtin.sample.read"
        effect_kind = "none"
        unattended = "allowed"

    with pytest.raises(CapabilityRegistryError, match="missing metadata: state_guard_id"):
        validate_tool_contract(cast(Any, MissingStateGuard()))


def test_tool_contract_rejects_invalid_metadata_combinations() -> None:
    class InvalidRisk(_GroupedTool):
        pass

    cast(Any, InvalidRisk).risk = "unsafe"

    class InvalidEffect(_GroupedTool):
        risk: ClassVar[Risk] = "mutating"
        effect_kind = "none"

    class InvalidUnattended(_GroupedTool):
        unattended = "sometimes"

    class ReadExternal(_ExternalTool):
        risk: ClassVar[Risk] = "read_only"

    class ExternalWithoutIdentity(_GroupedTool):
        risk: ClassVar[Risk] = "mutating"
        effect_kind = "external"

    for tool in (
        InvalidRisk(),
        InvalidEffect(),
        InvalidUnattended(),
        ReadExternal(),
        ExternalWithoutIdentity(),
    ):
        with pytest.raises(CapabilityRegistryError):
            validate_tool_contract(tool)


def test_named_state_guard_must_exist_and_only_wrap_ricky_state() -> None:
    class Guarded(_GroupedTool):
        risk: ClassVar[Risk] = "mutating"
        effect_kind = "ricky_state"
        state_guard_id = "test.guard"

    with pytest.raises(CapabilityRegistryError, match="unavailable state guard"):
        validate_tool_contract(Guarded())
    assert (
        validate_tool_contract(Guarded(), state_guards=StateGuardRegistry([_StateGuard()]))[
            "state_guard_id"
        ]
        == "test.guard"
    )

    class InvalidExternalGuard(_ExternalTool):
        state_guard_id = "test.guard"

    with pytest.raises(CapabilityRegistryError, match="not a Ricky-state mutation"):
        validate_tool_contract(
            InvalidExternalGuard(), state_guards=StateGuardRegistry([_StateGuard()])
        )


def test_forbidden_member_blocks_complete_group_but_destructive_allowed_does_not() -> None:
    class AllowedDestructive(_GroupedTool):
        name: ClassVar[str] = "allowed_delete"
        risk: ClassVar[Risk] = "destructive"
        capability_id = "builtin.sample.mutate"
        effect_kind = "ricky_state"

    class ForbiddenMutation(AllowedDestructive):
        name: ClassVar[str] = "forbidden_write"
        risk: ClassVar[Risk] = "mutating"
        unattended = "forbidden"

    allowed = build_capability_registry(
        [AllowedDestructive()],
        SkillRegistry(),
        capability_specs=(_sample_spec("mutate"),),
    ).require("builtin.sample.mutate")
    assert allowed.risk_class == "destructive"
    assert allowed.unattended_eligible

    blocked = build_capability_registry(
        [AllowedDestructive(), ForbiddenMutation()],
        SkillRegistry(),
        capability_specs=(_sample_spec("mutate"),),
    ).require("builtin.sample.mutate")
    assert not blocked.unattended_eligible
    assert blocked.unattended_blockers == ("forbidden_write: unattended forbidden",)
    decisions = resolve_capability_policy(
        build_capability_registry(
            [AllowedDestructive(), ForbiddenMutation()],
            SkillRegistry(),
            capability_specs=(_sample_spec("mutate"),),
        ),
        AgentCapabilityPolicySettings(),
        require_unattended=True,
    )
    assert not decisions[0].eligible
    assert "forbidden_write: unattended forbidden" in decisions[0].reasons


def test_metadata_changes_inventory_digest_but_not_callable_schema_digest() -> None:
    class Forbidden(_GroupedTool):
        unattended = "forbidden"

    class Fresh(_GroupedTool):
        review_mode = "fresh"

    allowed_registry = build_capability_registry(
        [_GroupedTool()], SkillRegistry(), capability_specs=(_sample_spec(),)
    )
    forbidden_registry = build_capability_registry(
        [Forbidden()], SkillRegistry(), capability_specs=(_sample_spec(),)
    )
    fresh_registry = build_capability_registry(
        [Fresh()], SkillRegistry(), capability_specs=(_sample_spec(),)
    )

    assert allowed_registry.digest() != forbidden_registry.digest()
    assert allowed_registry.digest() != fresh_registry.digest()
    assert fresh_registry.require("builtin.sample.read").resources[0].review_mode == "fresh"
    assert tool_contract_digest(_GroupedTool()) == tool_contract_digest(Forbidden())
    assert tool_contract_digest(_GroupedTool()) == tool_contract_digest(Fresh())


def test_resource_order_does_not_change_inventory_digest() -> None:
    class Second(_GroupedTool):
        name: ClassVar[str] = "second_read"

    first = build_capability_registry(
        [_GroupedTool(), Second()], SkillRegistry(), capability_specs=(_sample_spec(),)
    )
    reversed_order = build_capability_registry(
        [Second(), _GroupedTool()], SkillRegistry(), capability_specs=(_sample_spec(),)
    )

    assert first.digest() == reversed_order.digest()


def test_owners_distinguish_user_skills_from_bundled_skills(
    tmp_path: Path, bundled_root: Path
) -> None:
    user_root = tmp_path / ".ricky"
    bundle = user_root / "profiles" / "personal" / "skills" / "personal"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text(
        "---\nname: personal\ndescription: Personal skill\n---\nUse it.\n",
        encoding="utf-8",
    )
    shipped = bundled_root / "skills" / "shipped"
    shipped.mkdir(parents=True, exist_ok=True)
    (shipped / "SKILL.md").write_text(
        "---\nname: shipped\ndescription: Shipped skill\n---\nUse it.\n",
        encoding="utf-8",
    )
    settings = RickySettings.model_validate({"user_data_dir": str(user_root)})
    skills = discover_skills(
        settings=settings,
        profile_scope=settings.resolve_profile_scope(),
    )

    owners = derive_skill_owners(skills, settings=settings)

    assert owners == {"personal/personal": "user", "bundled/shipped": "bundled"}


def test_bundled_skill_capability_id_omits_the_profile_segment(bundled_root: Path) -> None:
    shipped = bundled_root / "skills" / "shipped"
    shipped.mkdir(parents=True, exist_ok=True)
    (shipped / "SKILL.md").write_text(
        "---\nname: shipped\ndescription: Shipped skill\n---\nUse it.\n",
        encoding="utf-8",
    )
    settings = RickySettings()
    skills = discover_skills(settings=settings, profile_scope=settings.resolve_profile_scope())

    registry = build_capability_registry(
        [],
        skills,
        capability_specs=(),
        skill_owners=derive_skill_owners(skills, settings=settings),
    )

    ids = {definition.id for definition in registry.definitions()}
    assert "bundled.skill.shipped" in ids


def test_unmapped_or_mixed_risk_tool_fails_closed() -> None:
    with pytest.raises(CapabilityRegistryError, match="no primary capability"):
        build_capability_registry([_Tool()], SkillRegistry(), capability_specs=())

    class BadGroupedTool(_Tool):
        capability_id = "builtin.bad.read"
        risk: ClassVar[Risk] = "mutating"
        effect_kind = "ricky_state"

    with pytest.raises(CapabilityRegistryError, match="read capability"):
        build_capability_registry(
            [BadGroupedTool()],
            SkillRegistry(),
            capability_specs=(
                CapabilitySpec(id="builtin.bad.read", owner="builtin", description="Bad group."),
            ),
        )


def test_default_eligibility_is_proposal_eligibility_and_exclusion_wins() -> None:
    registry = build_capability_registry(
        [_Tool()],
        SkillRegistry(),
        capability_specs=(),
        external_tool_owners={"personal_lookup": "user"},
    )
    default = resolve_capability_policy(registry, AgentCapabilityPolicySettings())
    assert default[0].eligible is True
    policy = AgentCapabilityPolicySettings(
        exclude_capabilities=["user.personal_lookup"],
        confirmation_required_capabilities=["user.personal_lookup"],
    )
    excluded = resolve_capability_policy(registry, policy)[0]
    assert excluded.eligible is False
    assert excluded.confirmation_required is False


def test_required_guardrail_must_have_registered_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_sandbox_runtime(monkeypatch)
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())

    async def check() -> None:
        async with build_capability_runtime(
            settings, session=session, project_root=tmp_path
        ) as runtime:
            policy = AgentCapabilityPolicySettings(
                guardrail_required_capabilities=["builtin.sandbox.reservation"]
            )
            missing = validate_capability_policy(
                runtime.capability_registry,
                GuardrailRegistry(),
                policy,
            )
            assert any(item.severity == "error" for item in missing)
            present = validate_capability_policy(
                runtime.capability_registry,
                GuardrailRegistry((SandboxGuardrailEvaluator(),)),
                policy,
            )
            assert present == ()

    import asyncio

    asyncio.run(check())


def test_guardrail_inventory_validation_is_independent_of_user_policy(
    tmp_path: Path,
) -> None:
    tool = SandboxReservationTool(_settings(tmp_path))
    registry = build_capability_registry(
        [tool],
        SkillRegistry(),
        capability_specs=(
            CapabilitySpec(
                id="builtin.sandbox.reservation",
                owner="builtin",
                description="Test reservation.",
                guardrail_schema_id="sandbox.reservation",
            ),
        ),
    )
    assert (
        validate_capability_inventory(registry, GuardrailRegistry((SandboxGuardrailEvaluator(),)))
        == ()
    )

    class MismatchedEvaluator(SandboxGuardrailEvaluator):
        tools = frozenset({"wrong_tool"})

    diagnostics = validate_capability_inventory(
        registry,
        GuardrailRegistry((cast(Any, MismatchedEvaluator()),)),
    )
    assert any("does not cover" in item.message for item in diagnostics)

    class CanonicalSupersetEvaluator(SandboxGuardrailEvaluator):
        tools = frozenset({SandboxReservationTool.name, "currently_uninstalled_tool"})

    assert (
        validate_capability_inventory(
            registry,
            GuardrailRegistry((cast(Any, CanonicalSupersetEvaluator()),)),
        )
        == ()
    )


def test_legacy_configured_capability_reports_provider_neutral_replacement() -> None:
    registry = build_capability_registry(
        [_Tool()],
        SkillRegistry(),
        capability_specs=(),
        external_tool_owners={"personal_lookup": "user"},
    )
    diagnostics = validate_capability_policy(
        registry,
        GuardrailRegistry(),
        AgentCapabilityPolicySettings(confirmation_required_capabilities=["builtin.gmail.send"]),
    )

    assert diagnostics[0].severity == "error"
    assert "replace with builtin.email.mutate" in diagnostics[0].message


def test_foreground_live_review_control_cannot_be_recursive_or_excluded(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())

    async def check() -> None:
        async with build_capability_runtime(
            settings, session=session, project_root=tmp_path
        ) as runtime:
            recursive = validate_foreground_live_policy(
                runtime.capability_registry,
                AgentCapabilityPolicySettings(
                    confirmation_required_capabilities=["builtin.authorization.review"]
                ),
            )
            assert any("cannot require its own" in item.message for item in recursive)
            unavailable = validate_foreground_live_policy(
                runtime.capability_registry,
                AgentCapabilityPolicySettings(
                    exclude_capabilities=["builtin.authorization.review"],
                    confirmation_required_capabilities=["builtin.project.read"],
                ),
            )
            assert any("need the source-bound" in item.message for item in unavailable)

    import asyncio

    asyncio.run(check())


def test_sandbox_guardrail_validates_structure_and_binds_turn_provenance() -> None:
    evaluator = SandboxGuardrailEvaluator()
    source = AuthenticatedSource(
        principal_id="telegram:owner:1",
        conversation_id="conversation_" + "a" * 32,
        message_id="inbound_" + "b" * 32,
        text_digest="c" * 64,
        text_snapshot=(
            "Reserve Venue One (venue-1) for 2 on 2026-08-18 from 18:00 to 19:00 "
            "America/Chicago using alex@example.com with no deposit."
        ),
        received_at=datetime.now(UTC),
    )
    constraints = {
        "venue_id": "venue-1",
        "venue_name": "Venue One",
        "party_size": 2,
        "local_date": "2026-08-18",
        "window_start": "18:00",
        "window_end": "19:00",
        "timezone": "America/Chicago",
        "account_identity": "alex@example.com",
        "deposit_limit_minor": 0,
    }
    accepted_with_unmatched_diagnostic_quote = evaluator.validate_collected(
        _collected_reservation_fields(
            evaluator, constraints, source, quote="invented by a tool result"
        ),
        (source,),
    )
    assert accepted_with_unmatched_diagnostic_quote.guardrail is not None
    accepted = evaluator.validate_collected(
        _collected_reservation_fields(evaluator, constraints, source, quote="Reserve Venue One"),
        (source,),
    )
    assert accepted.guardrail is not None
    assert accepted.guardrail.source_message_ids == (source.message_id,)

    interpreted = evaluator.normalize_field(
        GuardrailFieldProposal(
            field="venue_id",
            value="venue-2",
            source_quote=source.text_snapshot,
        ),
    )
    assert interpreted.accepted
    assert interpreted.value == "venue-2"


def test_sandbox_guardrail_normalizes_without_semantic_text_matching() -> None:
    evaluator = SandboxGuardrailEvaluator()
    zero_deposit = evaluator.normalize_field(
        GuardrailFieldProposal(
            field="deposit_limit_minor",
            value=0,
            source_quote="Words unrelated to deposits.",
        ),
    )
    timezone = evaluator.normalize_field(
        GuardrailFieldProposal(
            field="timezone",
            value="America/Chicago",
        ),
    )
    invalid_timezone = evaluator.normalize_field(
        GuardrailFieldProposal(field="timezone", value="Chicago time"),
    )

    assert zero_deposit.accepted and zero_deposit.value == 0
    assert timezone.accepted and timezone.value == "America/Chicago"
    assert not invalid_timezone.accepted
    assert invalid_timezone.question is not None


def test_guardrail_diagnostic_excerpt_is_optional_and_round_trips() -> None:
    proposal = GuardrailFieldProposal(field="timezone", value="America/Chicago")

    restored = GuardrailFieldProposal.model_validate_json(proposal.model_dump_json())

    assert restored == proposal
    assert restored.source_quote is None


def test_sandbox_guardrail_rejects_provenance_outside_authenticated_turns() -> None:
    evaluator = SandboxGuardrailEvaluator()
    source = AuthenticatedSource(
        principal_id="telegram:owner:1",
        conversation_id="conversation_" + "a" * 32,
        message_id="inbound_" + "b" * 32,
        text_digest="c" * 64,
        text_snapshot="Use structurally valid reservation values.",
        received_at=datetime.now(UTC),
    )
    fields = _collected_reservation_fields(
        evaluator,
        {
            "venue_id": "venue-1",
            "venue_name": "Venue One",
            "party_size": 2,
            "local_date": "2026-08-18",
            "window_start": "18:00",
            "window_end": "19:00",
            "timezone": "America/Chicago",
            "account_identity": "alex@example.com",
        },
        source,
        quote="diagnostic only",
    )

    rejected = evaluator.validate_collected(fields, ())

    assert rejected.reason == "collected guardrail field lacks authenticated turn provenance"
