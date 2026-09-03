"""Immutable, complete runtime contracts for new ad hoc executions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from ricky.capabilities.guardrails import CompiledGuardrail
from ricky.capabilities.registry import skill_bundle_digest
from ricky.capabilities.types import CapabilityResource
from ricky.config import RickySettings, user_data_path
from ricky.executions.browser import BrowserExecutionScope
from ricky.executions.spec import ExecutionBudget
from ricky.profiles import ProfileScope
from ricky.skills.registry import SkillRegistry

_CONTRACT_ID = re.compile(r"^contract_[0-9a-f]{32}$")
type ContextSourceKind = Literal[
    "linked_task",
    "task_artifacts",
    "memory_index",
    "project_context",
    "skill_instructions",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResolvedCapability(_FrozenModel):
    id: str = Field(min_length=3, max_length=300)
    version: int = Field(ge=1, le=1_000)
    resources: tuple[CapabilityResource, ...] = Field(min_length=1, max_length=100)
    confirmation_required: bool = False
    guardrail_required: bool = False
    authority_capability: str | None = Field(default=None, max_length=200)


class ResolvedTool(_FrozenModel):
    id: str = Field(min_length=1, max_length=300)
    contract_version: int = Field(ge=1, le=1_000)
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: str = Field(min_length=1, max_length=1_000)
    risk_class: Literal["read_only", "mutating", "destructive"]
    effect_kind: Literal["none", "ricky_state", "external"]
    unattended: Literal["allowed", "forbidden"]
    state_guard_id: str | None = Field(default=None, max_length=200)


class ResolvedSkill(_FrozenModel):
    id: str = Field(min_length=1, max_length=300)
    capability_id: str = Field(min_length=3, max_length=300)
    bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: str = Field(min_length=1, max_length=1_000)


class ContextSourceContract(_FrozenModel):
    kind: ContextSourceKind
    enabled: bool
    config: dict[str, JsonValue] = Field(default_factory=dict)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConfirmationRef(_FrozenModel):
    id: str = Field(pattern=r"^confirmation_[0-9a-f]{32}$")
    draft_id: str = Field(pattern=r"^draft_[0-9a-f]{32}$")
    draft_revision: int = Field(ge=1)
    principal_id: str = Field(min_length=1, max_length=500)
    source_message_id: str = Field(min_length=1, max_length=512)
    summary_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _times(self) -> ConfirmationRef:
        _utc(self.confirmed_at, "confirmed_at")
        _utc(self.expires_at, "expires_at")
        if self.expires_at <= self.confirmed_at:
            raise ValueError("confirmation expiry must follow confirmation time")
        return self


class ExecutionContract(_FrozenModel):
    """The exact immutable technical and authority ceiling for one request."""

    version: Literal[2, 3] = 2
    id: str
    parent_request_id: str | None = Field(default=None, pattern=r"^execution_[0-9a-f]{32}$")
    task_id: str
    task_revision: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=50_000)
    profile_scope: ProfileScope
    principal_id: str = Field(min_length=1, max_length=500)
    source_conversation_id: str = Field(min_length=1, max_length=512)
    source_message_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    route_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    notification_route: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=500)
    project_root_ref: str | None = Field(default=None, max_length=2_000)
    capabilities: tuple[ResolvedCapability, ...] = Field(min_length=1, max_length=100)
    tools: tuple[ResolvedTool, ...] = Field(max_length=100)
    skills: tuple[ResolvedSkill, ...] = Field(max_length=100)
    context_sources: tuple[ContextSourceContract, ...] = Field(max_length=20)
    budget: ExecutionBudget
    browser: BrowserExecutionScope | None = None
    guardrails: tuple[CompiledGuardrail, ...] = Field(max_length=20)
    confirmations: tuple[ConfirmationRef, ...] = Field(max_length=20)
    agent_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    route_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    expires_at: datetime | None = None
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _contract(self) -> ExecutionContract:
        if _CONTRACT_ID.fullmatch(self.id) is None:
            raise ValueError("invalid execution contract id")
        _utc(self.created_at, "created_at")
        if self.expires_at is not None:
            _utc(self.expires_at, "expires_at")
            if self.expires_at <= self.created_at:
                raise ValueError("contract expiry must follow creation")
        if self.version == 2 and self.browser is not None:
            raise ValueError("browser execution scopes require contract version 3")
        if self.browser is not None:
            selected_tools = {item.id for item in self.tools}
            if set(self.browser.allowed_tools) != selected_tools & set(self.browser.allowed_tools):
                raise ValueError("browser scope authorizes a tool outside the contract")
        if len(self.source_message_ids) != len(set(self.source_message_ids)):
            raise ValueError("contract source ids must be unique")
        if len({item.id for item in self.capabilities}) != len(self.capabilities):
            raise ValueError("contract capabilities must be unique")
        if len({item.id for item in self.tools}) != len(self.tools):
            raise ValueError("contract tools must be unique")
        if len({item.id for item in self.skills}) != len(self.skills):
            raise ValueError("contract skills must be unique")
        context_kinds = {item.kind for item in self.context_sources}
        required_context_kinds = {
            "linked_task",
            "task_artifacts",
            "memory_index",
            "project_context",
            "skill_instructions",
        }
        if context_kinds != required_context_kinds or len(self.context_sources) != len(
            required_context_kinds
        ):
            raise ValueError("contract must pin every context-source kind exactly once")
        project_context = next(
            (item for item in self.context_sources if item.kind == "project_context"),
            None,
        )
        if (
            project_context is not None
            and project_context.enabled
            and self.project_root_ref is None
        ):
            raise ValueError("enabled project context requires an exact project root")
        project_tool_ids = {
            "read_file",
            "list_dir",
            "glob_search",
            "grep_search",
            "write_file",
            "edit_file",
            "run_shell",
        }
        if self.project_root_ref is None and project_tool_ids & {item.id for item in self.tools}:
            raise ValueError("project tools require an exact project root")
        capability_ids = {item.id for item in self.capabilities}
        guarded = {item.capability_id for item in self.guardrails}
        if not guarded <= capability_ids:
            raise ValueError("contract guardrails must belong to selected capabilities")
        required = {item.id for item in self.capabilities if item.guardrail_required}
        if not required <= guarded:
            raise ValueError("every required guardrail must be present in the contract")
        confirmation_required = any(item.confirmation_required for item in self.capabilities)
        if confirmation_required != bool(self.confirmations):
            raise ValueError(
                "contract confirmation evidence must match selected policy requirements"
            )
        for confirmation in self.confirmations:
            if confirmation.principal_id != self.principal_id:
                raise ValueError("contract confirmation belongs to another principal")
            if confirmation.source_message_id not in self.source_message_ids:
                raise ValueError("contract confirmation source is outside the contract")
        resource_tools = {
            resource.id
            for capability in self.capabilities
            for resource in capability.resources
            if resource.kind == "tool"
        }
        resource_skills = {
            resource.id
            for capability in self.capabilities
            for resource in capability.resources
            if resource.kind == "skill"
        }
        if resource_tools != {item.id for item in self.tools}:
            raise ValueError("resolved tools differ from capability resources")
        if resource_skills != {item.id for item in self.skills}:
            raise ValueError("resolved skills differ from capability resources")
        if contract_digest(self) != self.digest:
            raise ValueError("execution contract digest does not match its resolved form")
        return self

    def job_spec(self):
        """Compile the exact contracted tool surface into the shared runner spec."""

        from ricky.jobs.spec import JobBudget, JobPermissions, JobSpec, JobTools

        selected_tools = self._execution_tool_ids()
        mutating = [
            tool.id
            for tool in self.tools
            if tool.id in selected_tools and tool.risk_class != "read_only"
        ]
        return JobSpec(
            version=3,
            name=f"contract-{self.id.removeprefix('contract_')[:16]}",
            description="Capability-compiled ad hoc execution contract.",
            provider=self.provider,
            model=self.model,
            goal=self.goal,
            tools=JobTools(allow=[tool.id for tool in self.tools if tool.id in selected_tools]),
            permissions=JobPermissions(allow_mutating=mutating),
            budget=JobBudget.model_validate(self.budget.model_dump(mode="json")),
        )

    def _execution_tool_ids(self) -> frozenset[str]:
        """Return the exact callable surface after browser guardrail narrowing.

        Capability resources remain fully pinned in the immutable contract so
        evaluator and inventory drift is detectable. The background runtime
        exposes only the browser tool subset explicitly compiled into the
        browser scope.
        """

        selected = {item.id for item in self.tools}
        if self.browser is None:
            return frozenset(selected)
        browser_capabilities = {
            "builtin.browser.read",
            "builtin.browser.interact",
            "builtin.browser.commit",
            "builtin.protected_value.use",
        }
        browser_inventory = {
            resource.id
            for capability in self.capabilities
            if capability.id in browser_capabilities
            for resource in capability.resources
            if resource.kind == "tool"
        }
        return frozenset((selected - browser_inventory) | set(self.browser.allowed_tools))


def contract_digest(contract: ExecutionContract | dict[str, object]) -> str:
    payload = (
        contract.model_dump(mode="json", exclude={"digest"})
        if isinstance(contract, ExecutionContract)
        else {key: value for key, value in contract.items() if key != "digest"}
    )
    # Version 2 predates the optional browser scope. Preserve its exact digest
    # shape so existing snapshots and newly compiled non-browser contracts do
    # not change merely because the version-3 field exists in the model.
    if payload.get("version") == 2 and payload.get("browser") is None:
        payload.pop("browser", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_execution_contract(**values: Any) -> ExecutionContract:
    """Validate a contract after deriving its digest from the complete resolved form."""

    payload = TypeAdapter(dict[str, Any]).dump_python(values, mode="json")
    payload["digest"] = "0" * 64
    payload["digest"] = contract_digest(payload)
    return ExecutionContract.model_validate(payload)


def context_source_digest(kind: str, enabled: bool, config: dict[str, JsonValue]) -> str:
    encoded = json.dumps(
        {"kind": kind, "enabled": enabled, "config": config},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def snapshot_contract(
    settings: RickySettings,
    contract: ExecutionContract,
    *,
    skills: SkillRegistry,
) -> Path:
    """Persist immutable contract/skill evidence without copying credentials."""

    root = user_data_path(settings) / settings.executions.contract_snapshot_dir
    target = root / contract.digest
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    _write_exact(target / "contract.json", contract.model_dump_json(indent=2).encode("utf-8"))
    selected = {item.id: item for item in contract.skills}
    for name, resolved in sorted(selected.items()):
        skill = skills.get(name)
        if skill is None:
            raise ValueError(f"selected skill is no longer installed: {name}")
        skill_root = target / "skills" / name
        skill_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        bundle_target = skill_root / "bundle"
        bundle_target.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = Path(skill.source_path).resolve()
        source_root = (
            Path(skill.bundle_path).resolve() if skill.bundle_path is not None else source.parent
        )
        candidates = (
            sorted(path for path in source_root.rglob("*") if path.is_file())
            if skill.bundle_path is not None
            else [source]
        )
        for source_file in candidates:
            if source_file.is_symlink():
                raise ValueError(f"skill snapshot refuses symbolic links: {name}")
            resolved_file = source_file.resolve()
            if not resolved_file.is_relative_to(source_root):
                raise ValueError(f"skill resource escapes its bundle: {name}")
            relative = (
                resolved_file.relative_to(source_root)
                if skill.bundle_path is not None
                else Path("SKILL.md")
            )
            destination = bundle_target / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_exact(destination, resolved_file.read_bytes())
        pinned_source = bundle_target / (
            source.relative_to(source_root) if skill.bundle_path is not None else "SKILL.md"
        )
        pinned_digest = skill_bundle_digest(str(pinned_source), str(bundle_target))
        if pinned_digest != resolved.bundle_digest:
            raise ValueError(f"skill snapshot digest differs from inventory: {name}")
        payload = {
            "name": skill.name,
            "description": skill.description,
            "body": skill.body,
            "bundle_digest": resolved.bundle_digest,
        }
        _write_exact(
            skill_root / "skill.json",
            json.dumps(payload, sort_keys=True, indent=2).encode("utf-8"),
        )
    return target


def load_contract_snapshot(settings: RickySettings, digest: str) -> ExecutionContract:
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid execution contract digest")
    path = (
        user_data_path(settings)
        / settings.executions.contract_snapshot_dir
        / digest
        / "contract.json"
    )
    contract = ExecutionContract.model_validate_json(path.read_text(encoding="utf-8"))
    if contract.digest != digest:
        raise ValueError("execution contract snapshot digest drift")
    return contract


def load_pinned_runtime(settings: RickySettings, contract: ExecutionContract):
    """Load provider-free exact runtime selections from immutable snapshots."""

    from ricky.jobs.spec import PinnedExecutionRuntime

    root = user_data_path(settings) / settings.executions.contract_snapshot_dir / contract.digest
    instructions: dict[str, str] = {}
    bundle_paths: dict[str, str] = {}
    for skill in contract.skills:
        payload = json.loads(
            (root / "skills" / skill.id / "skill.json").read_text(encoding="utf-8")
        )
        if payload.get("bundle_digest") != skill.bundle_digest:
            raise ValueError(f"execution skill snapshot digest drift: {skill.id}")
        body = payload.get("body")
        if not isinstance(body, str):
            raise ValueError(f"execution skill snapshot is malformed: {skill.id}")
        instructions[skill.id] = body
        bundle = (root / "skills" / skill.id / "bundle").resolve()
        if not bundle.is_relative_to(root.resolve()) or not bundle.is_dir():
            raise ValueError(f"execution skill snapshot escapes its contract: {skill.id}")
        source = bundle / "SKILL.md"
        if not source.is_file():
            raise ValueError(f"execution skill snapshot is missing SKILL.md: {skill.id}")
        if skill_bundle_digest(str(source), str(bundle)) != skill.bundle_digest:
            raise ValueError(f"execution skill bundle digest drift: {skill.id}")
        bundle_paths[skill.id] = str(bundle)
    memory_index = any(
        item.kind == "memory_index" and item.enabled for item in contract.context_sources
    )
    by_capability = {item.id: item for item in contract.capabilities}
    selected_tool_ids = set(contract.job_spec().tools.allow)
    selected_guardrails = tuple(
        guardrail
        for guardrail in contract.guardrails
        if any(
            resource.kind == "tool" and resource.id in selected_tool_ids
            for resource in by_capability[guardrail.capability_id].resources
        )
    )
    return PinnedExecutionRuntime(
        tool_digests={
            item.id: item.schema_digest for item in contract.tools if item.id in selected_tool_ids
        },
        tool_effect_kinds={
            item.id: item.effect_kind for item in contract.tools if item.id in selected_tool_ids
        },
        tool_unattended={
            item.id: item.unattended for item in contract.tools if item.id in selected_tool_ids
        },
        tool_state_guards={
            item.id: item.state_guard_id for item in contract.tools if item.id in selected_tool_ids
        },
        skill_digests={item.id: item.bundle_digest for item in contract.skills},
        skill_instructions=instructions,
        skill_bundle_paths=bundle_paths,
        guardrails=selected_guardrails,
        guardrail_tools={
            guardrail.capability_id: tuple(
                sorted(
                    resource.id
                    for resource in by_capability[guardrail.capability_id].resources
                    if resource.kind == "tool" and resource.id in selected_tool_ids
                )
            )
            for guardrail in selected_guardrails
        },
        authorized_mutating_tools=tuple(
            sorted(
                item.id
                for item in contract.tools
                if item.id in selected_tool_ids and item.risk_class != "read_only"
            )
        ),
        memory_index=memory_index,
    )


def _write_exact(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"immutable execution snapshot collision: {path.name}")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path: str | None = temporary
    published = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A same-directory hard link publishes the fully synced inode atomically
            # and, unlike os.replace(), never overwrites a concurrent immutable writer.
            os.link(temporary, path)
            published = True
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(f"immutable execution snapshot collision: {path.name}") from None
        os.unlink(temporary)
        temporary_path = None
        if published:
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_path)


def _utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
