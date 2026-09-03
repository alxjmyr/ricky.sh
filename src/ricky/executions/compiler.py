"""Deterministic live-draft and execution-contract compiler."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ricky.browser.guardrails import (
    browser_guardrail_constraints,
    compile_browser_execution_scope,
)
from ricky.browser.policy import canonical_origin
from ricky.browser.resources import (
    browser_resource_configuration_digest,
    require_browser_resource,
)
from ricky.capabilities import (
    AuthenticatedSource,
    CapabilityPolicyDecision,
    CapabilityRegistry,
    CollectedGuardrailField,
    GuardrailRegistry,
    capability_requires_project_root,
    resolve_capability_policy,
    validate_capability_policy,
)
from ricky.capabilities.policy import policy_digest
from ricky.config import (
    GatewayRouteSettings,
    PersistentBrowserResourceSettings,
    RickySettings,
    find_project_root,
)
from ricky.durable_tasks.scoped import ScopedDurableTaskStore, ScopedTaskArtifactStore
from ricky.durable_tasks.store import TaskStoreError
from ricky.durable_tasks.types import DurableTask
from ricky.executions.browser import (
    BrowserAttachmentPin,
    BrowserExecutionBudget,
    BrowserExecutionMode,
    BrowserExecutionScope,
    BrowserProtectedResourcePin,
    BrowserResourcePin,
)
from ricky.executions.contracts import (
    ConfirmationRef,
    ContextSourceContract,
    ContextSourceKind,
    ExecutionContract,
    ResolvedCapability,
    ResolvedSkill,
    ResolvedTool,
    build_execution_contract,
    context_source_digest,
    load_contract_snapshot,
    snapshot_contract,
)
from ricky.executions.drafts import (
    AdHocCancellation,
    AdHocConfirmation,
    AdHocDelegationCommand,
    AdHocExecutionProposal,
    AdHocGuardrailContinuation,
    ExecutionDraft,
    summary_digest,
)
from ricky.executions.spec import ExecutionBudget
from ricky.executions.store import ExecutionStore
from ricky.executions.types import is_retryable_execution_status
from ricky.profiles import ProfileScope
from ricky.protected_values import ProtectedValueBroker
from ricky.skills.registry import SkillRegistry


class ExecutionContractCompileError(RuntimeError):
    """A proposal cannot safely become a runnable contract."""


class CompileBinding(BaseModel):
    """Trusted values bound by gateway code, never supplied by the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(min_length=1, max_length=500)
    conversation_id: str = Field(min_length=1, max_length=512)
    route_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    notification_route: str = Field(min_length=1, max_length=200)
    profile_scope: ProfileScope
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=500)
    project_root_ref: str | None = Field(default=None, max_length=2_000)


class ExecutionContractCompiler:
    """Compile proposals through durable review into exact immutable contracts."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        capabilities: CapabilityRegistry,
        guardrails: GuardrailRegistry,
        skills: SkillRegistry,
        route: GatewayRouteSettings,
        binding: CompileBinding,
        store: ExecutionStore | None = None,
        protected_values: ProtectedValueBroker | None = None,
    ) -> None:
        self.settings = settings
        self.capabilities = capabilities
        self.guardrails = guardrails
        self.skills = skills
        self.route = route
        self.binding = binding
        self.store = store or ExecutionStore(settings)
        self.protected_values = protected_values
        if binding.provider != route.provider or binding.model != route.model:
            raise ValueError("compile binding provider/model must match the gateway route")
        if binding.profile_scope != route.profile_scope():
            raise ValueError("compile binding profile scope must match the gateway route")
        configured = settings.gateway.routes.get(binding.route_name)
        if configured is None:
            raise ValueError("compile binding route name is not configured")
        if _route_project_root(configured) != _route_project_root(route):
            raise ValueError("compile binding route name resolves to another project root")
        if policy_digest(settings.agents.ad_hoc_background, configured) != policy_digest(
            settings.agents.ad_hoc_background, route
        ):
            raise ValueError("compile binding route name resolves to another policy")
        if binding.project_root_ref != _route_project_root(route):
            raise ValueError("compile binding project root must match the gateway route")
        expected_notification_route = f"conversation:{binding.conversation_id}"
        if binding.notification_route != expected_notification_route:
            raise ValueError("compile binding notification route must match the conversation")

    async def review(
        self,
        proposal: AdHocDelegationCommand,
        *,
        source: AuthenticatedSource,
        now: datetime | None = None,
    ) -> ExecutionDraft:
        """Create or CAS-update one source-bound live review draft."""

        moment = now or datetime.now(UTC)
        self._validate_source(source)
        await self.store.initialize()

        current: ExecutionDraft | None = None
        if isinstance(proposal, AdHocExecutionProposal):
            task = await self._validate_task(
                proposal.task_id,
                proposal.expected_task_revision,
                self.binding.profile_scope,
            )
            await self._validate_retry(proposal.retry_of, task)
            goal = proposal.goal.strip()
            requested_capabilities = tuple(proposal.requested_capabilities)
            retry_of = proposal.retry_of
            proposed_guardrails = tuple(proposal.guardrails)
        else:
            current = await self.store.get_draft(
                proposal.draft_id, scope=self.binding.profile_scope
            )
            if current.revision != proposal.expected_draft_revision:
                raise ExecutionContractCompileError(
                    f"stale draft revision: expected {proposal.expected_draft_revision}, "
                    f"found {current.revision}"
                )
            if isinstance(proposal, AdHocCancellation):
                self._validate_cancellation(current, source)
                return await self.store.cancel_draft(
                    current.id, scope=self.binding.profile_scope, now=moment
                )
            self._validate_continuation(current, source, now=moment)
            if (
                isinstance(proposal, AdHocConfirmation)
                and current.status != "awaiting_confirmation"
            ):
                raise ExecutionContractCompileError(
                    "only a draft awaiting confirmation can be confirmed"
                )
            assert current.task_id is not None and current.task_revision is not None
            task = await self._validate_task(
                current.task_id,
                current.task_revision,
                self.binding.profile_scope,
            )
            await self._validate_retry(current.retry_of, task)
            goal = current.goal
            requested_capabilities = current.requested_capabilities
            retry_of = current.retry_of
            proposed_guardrails = (
                tuple(proposal.guardrails)
                if isinstance(proposal, AdHocGuardrailContinuation)
                else ()
            )

        decisions = self._policy_decisions()
        selected = self._selected_decisions(requested_capabilities, decisions)
        self._validate_resource_dependencies(selected)
        self._validate_effect_budget(selected)

        if current is not None and current.status == "ready":
            raise ExecutionContractCompileError(
                "a ready draft cannot accept more confirmation or guardrail input"
            )

        if current is not None and current.contract_id is not None:
            raise ExecutionContractCompileError(
                f"compiled draft cannot be reviewed from {current.status}"
            )

        if current is not None and isinstance(proposal, AdHocConfirmation):
            return await self._confirm(current, source=source, now=moment)

        sources = (
            tuple((*current.sources, source))
            if current is not None
            and source.message_id not in {item.message_id for item in current.sources}
            else (current.sources if current is not None else (source,))
        )
        existing_guardrails = {
            item.capability_id: item for item in (current.guardrails if current is not None else ())
        }
        proposed = {item.capability_id: item for item in proposed_guardrails}
        if not set(proposed) <= set(requested_capabilities):
            raise ExecutionContractCompileError(
                "guardrail continuation names an unrequested capability"
            )
        compiled = dict(existing_guardrails)
        collected = {
            (item.capability_id, item.field): item
            for item in (current.collected_guardrail_fields if current is not None else ())
        }
        questions: list[str] = []

        selected_by_id = {item.capability_id: item for item in selected}
        for capability_id, guardrail_proposal in proposed.items():
            decision = selected_by_id[capability_id]
            definition = self.capabilities.require(capability_id)
            if (
                not decision.guardrail_required
                and definition.authority_capability is None
                and self.guardrails.get(capability_id) is None
            ):
                raise ExecutionContractCompileError(
                    f"capability does not accept guardrail fields: {capability_id}"
                )
            evaluator = self.guardrails.get(capability_id)
            if evaluator is None:
                raise ExecutionContractCompileError(
                    f"capability has no registered guardrail evaluator: {capability_id}"
                )
            for field_proposal in guardrail_proposal.fields:
                if evaluator.intake_spec.get(field_proposal.field) is None:
                    raise ExecutionContractCompileError(
                        f"unknown guardrail field for {capability_id}: {field_proposal.field}"
                    )
                outcome = evaluator.normalize_field(field_proposal)
                if outcome.reason is not None:
                    raise ExecutionContractCompileError(outcome.reason)
                if outcome.question is not None:
                    questions.append(outcome.question)
                    compiled.pop(capability_id, None)
                    continue
                if not outcome.accepted:
                    raise ExecutionContractCompileError(
                        "guardrail evaluator returned no field normalization outcome"
                    )
                collected[(capability_id, field_proposal.field)] = CollectedGuardrailField(
                    capability_id=capability_id,
                    schema_id=evaluator.schema_id,
                    schema_version=evaluator.schema_version,
                    field=field_proposal.field,
                    value=outcome.value,
                    source_message_id=source.message_id,
                    source_text_digest=source.text_digest,
                    source_quote=field_proposal.source_quote,
                )
                compiled.pop(capability_id, None)

        for decision in selected:
            definition = self.capabilities.require(decision.capability_id)
            hard_scope_required = definition.guardrail_schema_id is not None
            if (
                not decision.guardrail_required
                and not hard_scope_required
                and decision.capability_id not in proposed
            ):
                continue
            evaluator = self.guardrails.get(decision.capability_id)
            if evaluator is None:
                raise ExecutionContractCompileError(
                    f"capability has no registered guardrail evaluator: {decision.capability_id}"
                )
            capability_fields = tuple(
                item
                for (capability_id, _), item in sorted(collected.items())
                if capability_id == decision.capability_id
            )
            if (
                not capability_fields
                and decision.capability_id in compiled
                and decision.capability_id not in proposed
            ):
                # Compatibility for a legacy draft. New
                # drafts always retain the field evidence used to compile.
                continue
            outcome = evaluator.validate_collected(capability_fields, sources)
            if outcome.reason is not None:
                raise ExecutionContractCompileError(outcome.reason)
            if outcome.questions:
                questions.extend(outcome.questions)
                continue
            assert outcome.guardrail is not None
            compiled[decision.capability_id] = outcome.guardrail

        confirmation_required = any(item.confirmation_required for item in selected)
        summary = self._confirmation_summary(
            requested_capabilities,
            tuple(compiled[key] for key in sorted(compiled)),
        )
        if questions:
            status = "collecting_guardrails"
            stored_summary = None
            stored_summary_digest = None
        elif confirmation_required:
            status = "awaiting_confirmation"
            stored_summary = summary
            stored_summary_digest = summary_digest(summary)
        else:
            status = "ready"
            stored_summary = summary
            stored_summary_digest = summary_digest(summary)

        revision = 1 if current is None else current.revision + 1
        created_at = current.created_at if current is not None else moment
        if current is None:
            assert isinstance(proposal, AdHocExecutionProposal)
            draft_id = _draft_id(proposal, source, self.binding.conversation_id)
        else:
            draft_id = current.id
        draft = ExecutionDraft(
            id=draft_id,
            target="ad_hoc_background",
            status=status,
            revision=revision,
            principal_id=self.binding.principal_id,
            conversation_id=self.binding.conversation_id,
            task_id=task.id,
            task_revision=task.revision,
            retry_of=retry_of,
            profile_scope=self.binding.profile_scope,
            goal=goal,
            requested_capabilities=requested_capabilities,
            sources=sources,
            collected_guardrail_fields=tuple(collected[key] for key in sorted(collected)),
            guardrails=tuple(compiled[key] for key in sorted(compiled)),
            pending_questions=tuple(dict.fromkeys(questions))[:20],
            confirmation_required=confirmation_required,
            confirmation_summary=stored_summary,
            confirmation_summary_digest=stored_summary_digest,
            confirmation=None,
            agent_policy_digest=self.settings.agents.ad_hoc_background.digest(),
            route_policy_digest=selected[0].policy_digest,
            inventory_digest=self.capabilities.digest(),
            created_at=created_at,
            updated_at=moment,
            expires_at=created_at + timedelta(seconds=self.settings.executions.draft_ttl_seconds),
        )
        if current is None:
            return await self.store.create_draft(draft, scope=self.binding.profile_scope)
        kind = "guardrails_updated" if current.status == "collecting_guardrails" else "ready"
        if draft.status == "awaiting_confirmation":
            kind = "confirmation_requested"
        return await self.store.update_draft(
            draft,
            scope=self.binding.profile_scope,
            expected_revision=current.revision,
            kind=kind,
            summary=(
                "Guardrail values updated"
                if draft.status != "awaiting_confirmation"
                else "Exact confirmation requested"
            ),
        )

    async def compile(
        self, draft: ExecutionDraft, *, now: datetime | None = None
    ) -> ExecutionContract:
        """Compile one ready draft, revalidating every current hard ceiling."""

        moment = now or datetime.now(UTC)
        if draft.status != "ready":
            raise ExecutionContractCompileError(f"execution draft is not ready: {draft.status}")
        if (
            draft.target != "ad_hoc_background"
            or draft.task_id is None
            or (draft.task_revision is None)
        ):
            raise ExecutionContractCompileError("only a task-bound ad hoc draft can compile")
        if draft.expires_at <= moment:
            raise ExecutionContractCompileError("execution draft expired before compilation")
        await self._validate_task(draft.task_id, draft.task_revision, draft.profile_scope)
        decisions = self._policy_decisions()
        selected = self._selected_decisions(draft.requested_capabilities, decisions)
        self._validate_resource_dependencies(selected)
        self._validate_effect_budget(selected)
        if draft.agent_policy_digest != self.settings.agents.ad_hoc_background.digest():
            raise ExecutionContractCompileError("background agent policy changed during review")
        if draft.route_policy_digest != selected[0].policy_digest:
            raise ExecutionContractCompileError("route policy changed during review")
        if draft.inventory_digest != self.capabilities.digest():
            raise ExecutionContractCompileError("capability inventory changed during review")
        if any(item.confirmation_required for item in selected):
            if draft.confirmation is None:
                raise ExecutionContractCompileError("required confirmation is missing")
            if draft.confirmation.expires_at <= moment:
                raise ExecutionContractCompileError("required confirmation expired")
        if draft.contract_id is not None:
            existing = await self.store.get_contract(draft.contract_id, scope=draft.profile_scope)
            if (
                existing.task_id != draft.task_id
                or existing.task_revision != draft.task_revision
                or existing.goal != draft.goal
                or existing.source_message_ids
                != tuple(source.message_id for source in draft.sources)
                or existing.agent_policy_digest != draft.agent_policy_digest
                or existing.route_policy_digest != draft.route_policy_digest
                or existing.inventory_digest != draft.inventory_digest
            ):
                raise ExecutionContractCompileError(
                    "draft's immutable execution contract no longer matches"
                )
            if load_contract_snapshot(self.settings, existing.digest) != existing:
                raise ExecutionContractCompileError(
                    "draft's immutable execution contract snapshot differs"
                )
            return existing

        resolved_capabilities: list[ResolvedCapability] = []
        tools: dict[str, ResolvedTool] = {}
        skills: dict[str, ResolvedSkill] = {}
        for decision in selected:
            definition = self.capabilities.require(decision.capability_id)
            resolved_capabilities.append(
                ResolvedCapability(
                    id=definition.id,
                    version=definition.version,
                    resources=definition.resources,
                    confirmation_required=decision.confirmation_required,
                    guardrail_required=decision.guardrail_required,
                    authority_capability=definition.authority_capability,
                )
            )
            for resource in definition.resources:
                if resource.kind == "tool":
                    assert resource.risk_class is not None
                    assert resource.effect_kind is not None
                    assert resource.unattended is not None
                    tools[resource.id] = ResolvedTool(
                        id=resource.id,
                        contract_version=resource.contract_version,
                        schema_digest=resource.digest,
                        provenance=resource.provenance,
                        risk_class=resource.risk_class,
                        effect_kind=resource.effect_kind,
                        unattended=resource.unattended,
                        state_guard_id=resource.state_guard_id,
                    )
                else:
                    skills[resource.id] = ResolvedSkill(
                        id=resource.id,
                        capability_id=definition.id,
                        bundle_digest=resource.digest,
                        provenance=resource.provenance,
                    )

        contexts = self._context_sources(
            capability_ids=set(draft.requested_capabilities),
            has_skills=bool(skills),
        )
        execution = self.settings.agents.ad_hoc_background.execution
        browser = await self._compile_browser_scope(draft.guardrails)
        contract = build_execution_contract(
            version=3 if browser is not None else 2,
            id=_contract_id(draft),
            parent_request_id=draft.retry_of,
            task_id=draft.task_id,
            task_revision=draft.task_revision,
            goal=draft.goal,
            profile_scope=draft.profile_scope,
            principal_id=draft.principal_id,
            source_conversation_id=draft.conversation_id,
            source_message_ids=tuple(source.message_id for source in draft.sources),
            route_name=self.binding.route_name,
            notification_route=self.binding.notification_route,
            provider=self.binding.provider,
            model=self.binding.model,
            project_root_ref=self.binding.project_root_ref,
            capabilities=tuple(sorted(resolved_capabilities, key=lambda item: item.id)),
            tools=tuple(tools[key] for key in sorted(tools)),
            skills=tuple(skills[key] for key in sorted(skills)),
            context_sources=contexts,
            budget=ExecutionBudget.model_validate(execution.model_dump(mode="json")),
            guardrails=draft.guardrails,
            confirmations=(draft.confirmation,) if draft.confirmation is not None else (),
            agent_policy_digest=draft.agent_policy_digest,
            route_policy_digest=draft.route_policy_digest,
            inventory_digest=draft.inventory_digest,
            authority_policy_digest=self.settings.authority.digest(),
            browser=browser,
            created_at=draft.updated_at,
            expires_at=(
                min(draft.expires_at, draft.confirmation.expires_at)
                if draft.confirmation is not None
                else draft.expires_at
            ),
        )
        snapshot_contract(self.settings, contract, skills=self.skills)
        await self.store.attach_contract(
            contract,
            scope=draft.profile_scope,
            draft_id=draft.id,
            expected_revision=draft.revision,
            now=moment,
        )
        return contract

    async def _compile_browser_scope(
        self,
        guardrails: tuple,
    ) -> BrowserExecutionScope | None:
        browser_guardrails = tuple(
            guardrail
            for guardrail in guardrails
            if guardrail.schema_id
            in {"browser.read", "browser.interact", "protected_value.use", "browser.commit"}
        )
        if not browser_guardrails:
            return None
        runtime = self.settings.resolve_profile_runtime_settings(self.binding.profile_scope)
        owner = runtime.browser.background
        if not runtime.browser.enabled or not owner.enabled or not owner.read_enabled:
            raise ExecutionContractCompileError("background browser ownership is disabled")
        constraints = tuple(
            browser_guardrail_constraints(guardrail) for guardrail in browser_guardrails
        )
        modes = {item.mode for item in constraints}
        if len(modes) != 1:
            raise ExecutionContractCompileError("browser capabilities selected different modes")
        mode = cast(BrowserExecutionMode, modes.pop())
        capabilities = {item.capability_id for item in constraints}
        if "builtin.browser.interact" in capabilities and not owner.interaction_enabled:
            raise ExecutionContractCompileError("background browser interaction is disabled")
        if "builtin.protected_value.use" in capabilities and not owner.protected_values_enabled:
            raise ExecutionContractCompileError("background protected-value use is disabled")
        if "builtin.browser.commit" in capabilities and not owner.commit_enabled:
            raise ExecutionContractCompileError("background browser commits are disabled")

        requested_ephemeral = any(item.allow_ephemeral for item in constraints)
        if requested_ephemeral and not owner.allow_ephemeral:
            raise ExecutionContractCompileError("ephemeral background browsers are disabled")
        requested_public_research = any(item.allow_public_https_research for item in constraints)
        if requested_public_research and not owner.allow_public_https_research:
            raise ExecutionContractCompileError("public HTTPS background research is disabled")
        requested_private_origins = {
            origin for item in constraints for origin in item.private_origin_ceiling
        }
        installation_private_origins = {
            canonical_origin(origin) for origin in runtime.browser.allowed_private_origins
        }
        if not requested_private_origins <= installation_private_origins:
            raise ExecutionContractCompileError(
                "private browser origin is outside the installation ceiling"
            )

        reviewed_resource_origins = {
            selection.resource.qualified: selection.origins
            for item in constraints
            for selection in item.authenticated_origins
        }

        selected_resources = {ref.qualified: ref for item in constraints for ref in item.resources}
        resource_pins: list[BrowserResourcePin] = []
        for qualified in sorted(selected_resources):
            ref = selected_resources[qualified]
            resolved = require_browser_resource(runtime, scope=self.binding.profile_scope, ref=ref)
            if (
                resolved is None
                or not isinstance(resolved.settings, PersistentBrowserResourceSettings)
                or resolved.settings.headless is not True
            ):
                raise ExecutionContractCompileError(
                    "background browser resource must be owned, persistent, and headless: "
                    f"{qualified}"
                )
            resource_pins.append(
                BrowserResourcePin(
                    resource=ref,
                    kind="persistent",
                    configuration_digest=browser_resource_configuration_digest(resolved),
                    authenticated_origin_ceiling=reviewed_resource_origins[qualified],
                )
            )

        attachment_ids = {
            attachment_id for item in constraints for attachment_id in item.attachment_ids
        }
        upload_selected = any("browser_upload" in item.allowed_tools for item in constraints)
        if upload_selected != bool(attachment_ids):
            raise ExecutionContractCompileError(
                "background browser upload requires exact task artifact attachment ids"
            )
        attachment_pins: list[BrowserAttachmentPin] = []
        if attachment_ids:
            tasks = await ScopedDurableTaskStore.create(
                runtime,
                scope=self.binding.profile_scope,
            )
            artifacts = ScopedTaskArtifactStore(tasks)
            for attachment_id in sorted(attachment_ids):
                try:
                    prefix, profile, task_id, artifact_path = attachment_id.split("/", 3)
                except ValueError as exc:
                    raise ExecutionContractCompileError(
                        "browser attachment ids must use task/PROFILE/TASK_ID/PATH"
                    ) from exc
                if prefix != "task" or not artifact_path:
                    raise ExecutionContractCompileError(
                        "browser attachment ids must use task/PROFILE/TASK_ID/PATH"
                    )
                try:
                    task = await tasks.get_task(task_id)
                    if task.profile != profile or not self.binding.profile_scope.includes(profile):
                        raise ExecutionContractCompileError(
                            f"browser attachment is outside its exact profile: {attachment_id}"
                        )
                    entry = await artifacts.inspect(task_id, artifact_path)
                    attachment_pins.append(
                        BrowserAttachmentPin(
                            id=attachment_id,
                            profile=task.profile,
                            task_id=task.id,
                            artifact_path=entry.path,
                            sha256=entry.sha256,
                            byte_count=entry.size,
                        )
                    )
                except ExecutionContractCompileError:
                    raise
                except (TaskStoreError, ValueError, OSError) as exc:
                    raise ExecutionContractCompileError(
                        f"browser attachment is unavailable: {attachment_id}"
                    ) from exc

        selected_protected = {
            selection.resource.qualified: selection
            for item in constraints
            for selection in item.protected_values
        }
        protected_pins: list[BrowserProtectedResourcePin] = []
        for qualified in sorted(selected_protected):
            if self.protected_values is None:
                raise ExecutionContractCompileError(
                    "protected-value metadata is unavailable to this compiler"
                )
            selection = selected_protected[qualified]
            catalog = await self.protected_values.catalog(ref=selection.resource)
            if len(catalog) != 1:
                raise ExecutionContractCompileError(
                    f"protected resource is unavailable: {qualified}"
                )
            descriptor = catalog[0]
            if not descriptor.enabled or not descriptor.policy.unattended_allowed:
                raise ExecutionContractCompileError(
                    f"protected resource forbids unattended use: {qualified}"
                )
            for field in selection.fields:
                descriptor.field(field)
            commit_limit = (
                descriptor.policy.max_unattended_commits_per_execution
                if descriptor.policy.unattended_commit_allowed
                else 0
            )
            protected_pins.append(
                BrowserProtectedResourcePin(
                    resource=descriptor.ref,
                    revision=descriptor.revision,
                    fields=selection.fields,
                    materialization_limit=min(
                        descriptor.policy.max_unattended_materializations_per_execution,
                        owner.budget.protected_materializations,
                    ),
                    commit_limit=min(commit_limit, owner.budget.transaction_commits),
                )
            )

        launch_profiles = (
            {item.resource.profile for item in resource_pins}
            if resource_pins
            else {self.binding.profile_scope.primary}
        )
        visual_selected = any(
            "browser_visual_snapshot" in item.allowed_tools for item in constraints
        )
        allowed_screenshot_profiles: set[str] = set()
        for profile in launch_profiles:
            configured = runtime.profile_configs.get(profile)
            if (
                configured is not None
                and configured.browser is not None
                and self.binding.provider in configured.browser.screenshot_allowed_providers
            ):
                allowed_screenshot_profiles.add(profile)
        if visual_selected and allowed_screenshot_profiles != launch_profiles:
            raise ExecutionContractCompileError(
                "masked browser screenshots are not allowed for the selected provider"
            )

        budget = BrowserExecutionBudget.model_validate(
            owner.budget.model_dump(mode="json"), strict=True
        )
        try:
            return compile_browser_execution_scope(
                mode=mode,
                guardrails=browser_guardrails,
                budget=budget,
                resource_pins=tuple(resource_pins),
                attachment_pins=tuple(attachment_pins),
                protected_resource_pins=tuple(protected_pins),
            )
        except ValueError as exc:
            raise ExecutionContractCompileError(str(exc)) from exc

    async def _confirm(
        self,
        draft: ExecutionDraft,
        *,
        source: AuthenticatedSource,
        now: datetime,
    ) -> ExecutionDraft:
        if draft.status != "awaiting_confirmation":
            raise ExecutionContractCompileError(
                f"draft is not awaiting confirmation: {draft.status}"
            )
        if not _is_affirmative(source.text_snapshot):
            raise ExecutionContractCompileError(
                "confirmation message must be an explicit standalone affirmative"
            )
        assert draft.confirmation_summary_digest is not None
        confirmation = ConfirmationRef(
            id=f"confirmation_{uuid4().hex}",
            draft_id=draft.id,
            draft_revision=draft.revision,
            principal_id=draft.principal_id,
            source_message_id=source.message_id,
            summary_digest=draft.confirmation_summary_digest,
            confirmed_at=now,
            expires_at=min(
                draft.expires_at,
                now + timedelta(seconds=self.settings.executions.confirmation_ttl_seconds),
            ),
        )
        sources = (
            draft.sources
            if source.message_id in {item.message_id for item in draft.sources}
            else (*draft.sources, source)
        )
        confirmed = draft.model_copy(
            update={
                "status": "ready",
                "revision": draft.revision + 1,
                "sources": sources,
                "confirmation": confirmation,
                "updated_at": now,
            }
        )
        return await self.store.update_draft(
            confirmed,
            scope=draft.profile_scope,
            expected_revision=draft.revision,
            kind="confirmed",
            summary="Exact normalized execution proposal confirmed",
        )

    def _policy_decisions(self) -> dict[str, CapabilityPolicyDecision]:
        diagnostics = validate_capability_policy(
            self.capabilities,
            self.guardrails,
            self.settings.agents.ad_hoc_background,
            route=self.route,
        )
        errors = [item for item in diagnostics if item.severity == "error"]
        if errors:
            raise ExecutionContractCompileError(
                "; ".join(f"{item.capability_id}: {item.message}" for item in errors)
            )
        decisions = resolve_capability_policy(
            self.capabilities,
            self.settings.agents.ad_hoc_background,
            route=self.route,
            require_unattended=True,
        )
        return {item.capability_id: item for item in decisions}

    @staticmethod
    def _selected_decisions(
        requested: tuple[str, ...],
        decisions: dict[str, CapabilityPolicyDecision],
    ) -> tuple[CapabilityPolicyDecision, ...]:
        selected: list[CapabilityPolicyDecision] = []
        for capability_id in requested:
            decision = decisions.get(capability_id)
            if decision is None:
                raise ExecutionContractCompileError(
                    f"unknown or inactive capability: {capability_id}"
                )
            if not decision.eligible:
                raise ExecutionContractCompileError(
                    f"capability is not eligible: {capability_id} ({'; '.join(decision.reasons)})"
                )
            selected.append(decision)
        return tuple(selected)

    def _confirmation_summary(
        self,
        requested: tuple[str, ...],
        guardrails: tuple,
    ) -> str:
        lines = [
            (
                "Task-specific background execution using "
                f"{self.binding.provider}/{self.binding.model}."
            ),
            "Capabilities:",
            *(f"- {capability_id}" for capability_id in requested),
        ]
        lines.append(
            "Approval authorizes the background worker to use these capabilities "
            "for this task within the contract and runtime ceilings."
        )
        if guardrails:
            lines.append("Guardrails:")
            lines.extend(f"- {item.summary}" for item in guardrails)
        execution = self.settings.agents.ad_hoc_background.execution
        lines.append(
            "Runtime ceiling: "
            f"{execution.wall_clock_seconds:g}s, {execution.iterations} iterations, "
            f"{execution.effect_calls} effect calls."
        )
        return "\n".join(lines)[:8_000]

    def _validate_resource_dependencies(
        self,
        selected: tuple[CapabilityPolicyDecision, ...],
    ) -> None:
        """Reject project-scoped resources when the route has no project binding."""

        if self.binding.project_root_ref is not None:
            return
        for decision in selected:
            definition = self.capabilities.require(decision.capability_id)
            if capability_requires_project_root(definition):
                raise ExecutionContractCompileError(
                    f"capability requires a configured project root: {definition.id}"
                )

    def _validate_effect_budget(
        self,
        selected: tuple[CapabilityPolicyDecision, ...],
    ) -> None:
        """A contracted external mutation needs at least one ledger reservation."""

        external_mutations = [
            definition.id
            for decision in selected
            for definition in [self.capabilities.require(decision.capability_id)]
            if any(
                resource.kind == "tool" and resource.effect_kind == "external"
                for resource in definition.resources
            )
        ]
        if external_mutations and self.settings.agents.ad_hoc_background.execution.effect_calls < 1:
            raise ExecutionContractCompileError(
                "external-effect capabilities require a positive background effect-call "
                "budget: " + ", ".join(sorted(external_mutations))
            )

    def _context_sources(
        self, *, capability_ids: set[str], has_skills: bool
    ) -> tuple[ContextSourceContract, ...]:
        specs: list[tuple[ContextSourceKind, bool, dict[str, JsonValue]]] = [
            ("linked_task", True, {"revision": "pinned"}),
            ("task_artifacts", True, {"max_files": 5, "max_chars": 20_000}),
            ("memory_index", "builtin.memory.read" in capability_ids, {}),
            (
                "project_context",
                self.binding.project_root_ref is not None
                and "builtin.project.read" in capability_ids,
                {"root_ref": self.binding.project_root_ref},
            ),
            ("skill_instructions", has_skills, {"selected_only": True}),
        ]
        return tuple(
            ContextSourceContract(
                kind=kind,
                enabled=enabled,
                config=config,
                digest=context_source_digest(kind, enabled, config),
            )
            for kind, enabled, config in specs
        )

    async def _validate_task(self, task_id: str, revision: int, scope: ProfileScope) -> DurableTask:
        store = await ScopedDurableTaskStore.create(self.settings, scope=scope)
        task = await store.get_task(task_id)
        if task.revision != revision:
            raise ExecutionContractCompileError(
                f"durable task revision changed: expected {revision}, found {task.revision}"
            )
        if task.status in {"completed", "cancelled"}:
            raise ExecutionContractCompileError("durable task is closed")
        if task.execution_mode == "user":
            raise ExecutionContractCompileError("user-owned tasks cannot be delegated")
        return task

    async def _validate_retry(
        self,
        request_id: str | None,
        task: DurableTask,
    ) -> None:
        if request_id is None:
            return
        await self.store.initialize()
        request = await self.store.get(request_id, scope=self.binding.profile_scope)
        if not is_retryable_execution_status(request.status):
            if request.status == "uncertain":
                raise ExecutionContractCompileError(
                    "uncertain execution must be resolved before it can be retried"
                )
            raise ExecutionContractCompileError("execution status is not retryable")
        if request.kind != "ad_hoc":
            raise ExecutionContractCompileError("named jobs use their authored retry path")
        if request.task_id != task.id or request.task_revision != task.revision:
            raise ExecutionContractCompileError("retry source belongs to another task revision")

    def _validate_source(self, source: AuthenticatedSource) -> None:
        if source.principal_id != self.binding.principal_id:
            raise ExecutionContractCompileError("source principal does not match binding")
        if source.conversation_id != self.binding.conversation_id:
            raise ExecutionContractCompileError("source conversation does not match binding")

    def _validate_continuation(
        self,
        current: ExecutionDraft,
        source: AuthenticatedSource,
        *,
        now: datetime,
    ) -> None:
        self._validate_source(source)
        if current.status not in {"collecting_guardrails", "awaiting_confirmation"}:
            raise ExecutionContractCompileError(
                f"execution draft cannot be continued from {current.status}"
            )
        if current.target != "ad_hoc_background":
            raise ExecutionContractCompileError("draft is not an ad hoc delegation")
        if current.principal_id != source.principal_id:
            raise ExecutionContractCompileError("draft belongs to another principal")
        if current.expires_at <= now:
            raise ExecutionContractCompileError("execution draft expired")
        if current.agent_policy_digest != self.settings.agents.ad_hoc_background.digest():
            raise ExecutionContractCompileError("background agent policy changed during review")
        if current.route_policy_digest != policy_digest(
            self.settings.agents.ad_hoc_background, self.route
        ):
            raise ExecutionContractCompileError("gateway route policy changed during review")
        if current.inventory_digest != self.capabilities.digest():
            raise ExecutionContractCompileError("capability inventory changed during review")

    def _validate_cancellation(
        self,
        current: ExecutionDraft,
        source: AuthenticatedSource,
    ) -> None:
        """Permit an authenticated owner to close an active review fail-safely."""

        self._validate_source(source)
        if current.target != "ad_hoc_background":
            raise ExecutionContractCompileError("draft is not an ad hoc delegation")
        if current.principal_id != source.principal_id:
            raise ExecutionContractCompileError("draft belongs to another principal")


def _is_affirmative(text: str) -> bool:
    normalized = " ".join(text.strip().lower().rstrip(".! ").split())
    return normalized in {"yes", "confirm", "confirmed", "proceed", "approve", "approved"}


def _contract_id(draft: ExecutionDraft) -> str:
    identity = f"{draft.id}:{draft.revision}:{draft.inventory_digest}"
    return "contract_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _draft_id(
    proposal: AdHocExecutionProposal,
    source: AuthenticatedSource,
    conversation_id: str,
) -> str:
    payload = {
        "target": "ad_hoc_background",
        "conversation_id": conversation_id,
        "source_message_id": source.message_id,
        "task_id": proposal.task_id,
        "task_revision": proposal.expected_task_revision,
        "retry_of": proposal.retry_of,
        "goal": proposal.goal.strip(),
        "capabilities": proposal.requested_capabilities,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "draft_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _route_project_root(route: GatewayRouteSettings) -> str | None:
    if route.project_root is None:
        return None
    configured = Path(route.project_root).expanduser()
    if not configured.is_absolute():
        configured = find_project_root() / configured
    return str(configured.resolve())
