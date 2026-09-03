"""Workflow compiler tests."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from ricky.config import RickySettings, WorkflowSettings
from ricky.profiles import ProfileScope
from ricky.tools import Risk, ToolContext, ToolRegistry, ToolResult
from ricky.workflows.compile import compile_workflow
from ricky.workflows.describe import describe_workflow
from ricky.workflows.registry import discover_workflows, find_workflow_bundle
from ricky.workflows.spec import WorkflowSpec


class QueryParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str


class SourceResult(BaseModel):
    query: str
    items: list[dict[str, JsonValue]]


class SourceTool:
    name: ClassVar[str] = "source"
    description: ClassVar[str] = "Return typed records."
    Params: ClassVar[type[BaseModel]] = QueryParams
    Result: ClassVar[type[BaseModel]] = SourceResult
    risk: ClassVar[Risk] = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None
    safe_replay: ClassVar[bool] = True

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = ctx
        parsed = QueryParams.model_validate(params)
        data = {"query": parsed.query, "items": [{"id": "one"}]}
        return ToolResult(content="display can differ", data=data)


class ReaderResult(BaseModel):
    text: str


class ReaderTool:
    name: ClassVar[str] = "reader"
    description: ClassVar[str] = "Read one safe local value."
    Params: ClassVar[type[BaseModel]] = QueryParams
    Result: ClassVar[type[BaseModel]] = ReaderResult
    risk: ClassVar[Risk] = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = params, ctx
        return ToolResult(content="read", data={"text": "value"})


class EffectTool(ReaderTool):
    name: ClassVar[str] = "effect"
    risk: ClassVar[Risk] = "mutating"
    effect_kind = "ricky_state"


class UntypedTool:
    name: ClassVar[str] = "untyped"
    description: ClassVar[str] = "Return display text only."
    Params: ClassVar[type[BaseModel]] = QueryParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = params, ctx
        return ToolResult(content="display only")


def _registry() -> ToolRegistry:
    return ToolRegistry([SourceTool(), ReaderTool(), EffectTool(), UntypedTool()])


def _valid_graph() -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "generic-dag",
            "description": "A context-neutral graph.",
            "args": {"seed": {"type": "string", "description": "Input seed."}},
            "schemas": {
                "decision": {
                    "type": "object",
                    "required": ["flag", "label"],
                    "properties": {
                        "flag": {"type": "boolean"},
                        "label": {"type": "string"},
                    },
                },
                "note": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
            },
            "steps": [
                {
                    "id": "source",
                    "kind": "tool",
                    "tool": "source",
                    "args": {"query": {"ref": "trigger.seed"}},
                },
                {
                    "id": "decide",
                    "kind": "model",
                    "instruction": "Return a decision.",
                    "inputs": {"seed": {"ref": "trigger.seed"}},
                    "result_schema": "decision",
                },
                {
                    "id": "investigate",
                    "kind": "agent",
                    "instruction": "Read supporting data.",
                    "inputs": {"seed": {"ref": "trigger.seed"}},
                    "result_schema": "note",
                    "tools": ["reader"],
                },
                {
                    "id": "merge",
                    "kind": "data",
                    "needs": ["source", "decide", "investigate"],
                    "operator": "merge",
                    "args": {
                        "values": [
                            {"ref": "steps.source.output"},
                            {"ref": "steps.decide.output"},
                            {"ref": "steps.investigate.output"},
                        ],
                        "mode": "objects",
                    },
                },
                {
                    "id": "conditional",
                    "kind": "message",
                    "needs": ["decide"],
                    "when": {"ref": "steps.decide.output.flag", "is_true": True},
                    "message": "flagged",
                },
                {
                    "id": "each",
                    "kind": "foreach",
                    "needs": ["source"],
                    "collection": {"ref": "steps.source.output.items"},
                    "item_key": {"ref": "item.source.id"},
                    "body": [
                        {
                            "id": "note",
                            "kind": "message",
                            "message": {
                                "format": "item {key}",
                                "values": {"key": {"ref": "item.key"}},
                            },
                        }
                    ],
                },
                {
                    "id": "report",
                    "kind": "message",
                    "needs": ["merge", "conditional", "each"],
                    "dependency_policy": "terminal",
                    "message": {
                        "format": "merged {value}",
                        "values": {"value": {"ref": "steps.merge.output.value"}},
                    },
                },
            ],
        }
    )


def test_compiler_accepts_roots_fan_out_fan_in_condition_and_foreach() -> None:
    result = compile_workflow(_valid_graph(), tool_registry=_registry())

    assert result.ok
    assert result.graph is not None
    assert result.graph.roots == ["source", "decide", "investigate"]
    assert result.graph.body_roots == {"each": ["note"]}
    assert result.graph.order[-1] == "report"
    assert len(result.graph.fingerprint) == 64
    assert result.graph == type(result.graph).model_validate_json(result.graph.model_dump_json())


def test_compiler_validates_foreach_output_projection_references() -> None:
    raw = _valid_graph().model_dump(mode="json", by_alias=True)
    foreach = next(step for step in raw["steps"] if step["id"] == "each")
    foreach["outputs"] = {
        "note": {"ref": "item.steps.note.output"},
        "seed": {"ref": "trigger.seed"},
        "source": {"ref": "item.source"},
    }

    valid = compile_workflow(
        WorkflowSpec.model_validate(raw),
        tool_registry=_registry(),
    )
    assert valid.ok
    assert valid.graph is not None
    assert 'outputs: {"note": {"ref": "item.steps.note.output"}' in (describe_workflow(valid.graph))

    foreach["outputs"] = {"missing": {"ref": "item.steps.missing.output"}}
    invalid = compile_workflow(
        WorkflowSpec.model_validate(raw),
        tool_registry=_registry(),
    )

    assert invalid.graph is None
    assert any("unknown item step reference" in error for error in invalid.errors)


def test_compiler_fingerprint_is_stable_and_changes_with_graph() -> None:
    first = compile_workflow(_valid_graph(), tool_registry=_registry()).graph
    second = compile_workflow(_valid_graph(), tool_registry=_registry()).graph
    changed_spec = _valid_graph().model_copy(update={"description": "Changed."})
    changed = compile_workflow(changed_spec, tool_registry=_registry()).graph

    assert first is not None and second is not None and changed is not None
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != changed.fingerprint


def test_compiler_rejects_cycle_and_reports_all_independent_contract_errors() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "invalid-dag",
            "description": "Contains independent errors.",
            "schemas": {
                "text": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                }
            },
            "steps": [
                {
                    "id": "a",
                    "kind": "message",
                    "needs": ["b"],
                    "message": {"ref": "item.source"},
                },
                {"id": "b", "kind": "message", "needs": ["a"], "message": "b"},
                {"id": "bad-tool", "kind": "tool", "tool": "missing"},
                {"id": "untyped", "kind": "tool", "tool": "untyped"},
                {
                    "id": "bad-agent",
                    "kind": "agent",
                    "instruction": "Investigate.",
                    "result_schema": "text",
                    "tools": ["effect"],
                },
                {"id": "bad-data", "kind": "data", "operator": "special-case"},
                {
                    "id": "bad-model",
                    "kind": "model",
                    "instruction": "Return data.",
                    "result_schema": "missing-schema",
                },
            ],
        }
    )

    result = compile_workflow(spec, tool_registry=_registry())
    text = "\n".join(result.errors)

    assert result.graph is None
    assert "dependency cycle" in text
    assert "item reference is valid only" in text
    assert "unknown tool 'missing'" in text
    assert "no declared Result model" in text
    assert "only read_only is allowed" in text
    assert "unknown data operator" in text
    assert "unknown result schema" in text


def test_compiler_rejects_known_incompatible_condition_reference() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "bad-condition",
            "description": "Wrong condition type.",
            "schemas": {
                "text": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                }
            },
            "steps": [
                {
                    "id": "model",
                    "kind": "model",
                    "instruction": "Return text.",
                    "result_schema": "text",
                },
                {
                    "id": "use",
                    "kind": "message",
                    "needs": ["model"],
                    "when": {"ref": "steps.model.output.value", "is_true": True},
                    "message": "wrong",
                },
            ],
        }
    )

    result = compile_workflow(spec, tool_registry=_registry())

    assert any("requires boolean" in error for error in result.errors)


def test_compiler_rejects_tool_retries_without_explicit_replay_safety() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "unsafe-retries",
            "description": "Reject unsafe retry contracts.",
            "steps": [
                {
                    "id": "read",
                    "kind": "tool",
                    "tool": "reader",
                    "args": {"query": "value"},
                    "retry": {"max_attempts": 2, "on": ["tool_error"]},
                },
                {
                    "id": "effect",
                    "kind": "tool",
                    "tool": "effect",
                    "args": {"query": "value"},
                    "retry": {"max_attempts": 2, "on": ["tool_error"]},
                },
            ],
        }
    )

    result = compile_workflow(spec, tool_registry=_registry())
    text = "\n".join(result.errors)

    assert "read-only tool retry requires declared safe_replay" in text
    assert "effect tool retry requires declared idempotent replay and an idempotency key" in text


def test_compiler_enforces_model_attempt_and_agent_iteration_limits() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "unbounded-model-work",
            "description": "Reject model work above configured limits.",
            "schemas": {
                "text": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                }
            },
            "steps": [
                {
                    "id": "model",
                    "kind": "model",
                    "instruction": "Return data.",
                    "result_schema": "text",
                    "retry": {"max_attempts": 3, "on": ["invalid_output"]},
                },
                {
                    "id": "agent",
                    "kind": "agent",
                    "instruction": "Return data.",
                    "result_schema": "text",
                    "max_iterations": 9,
                },
            ],
        }
    )

    result = compile_workflow(
        spec,
        tool_registry=_registry(),
        settings=WorkflowSettings(model_attempts=2, agent_iterations=8),
    )
    text = "\n".join(result.errors)

    assert "retry max_attempts 3 exceeds configured maximum 2" in text
    assert "max_iterations 9 exceeds configured maximum 8" in text


def test_discovery_loads_version_two_and_rejects_other_versions(bundled_root: Path) -> None:
    root = bundled_root / "workflows"
    v1 = root / "legacy"
    v1.mkdir(parents=True)
    (v1 / "workflow.toml").write_text(
        """
name = "legacy"
description = "Legacy graph."
entry = "done"
[[steps]]
id = "done"
kind = "message"
template = "done"
""",
        encoding="utf-8",
    )
    v2 = root / "modern"
    v2.mkdir()
    (v2 / "workflow.toml").write_text(
        """
version = 2
name = "modern"
description = "Modern graph."
[[steps]]
id = "done"
kind = "message"
message = "done"
""",
        encoding="utf-8",
    )
    bad = root / "future"
    bad.mkdir()
    (bad / "workflow.toml").write_text(
        'version = 99\nname = "future"\ndescription = "Unknown."\n',
        encoding="utf-8",
    )

    registry = discover_workflows(
        profile_scope=ProfileScope.create("personal"),
        skill_names=set(),
        tool_registry=ToolRegistry([]),
    )

    assert registry.get("legacy") is None
    assert registry.get("modern") is not None
    assert [spec.name for spec in registry.workflows()] == ["modern"]
    assert "modern:" in registry.prompt_listing()
    assert len(registry.errors) == 2
    messages = {Path(error.source_path).parent.name: error.message for error in registry.errors}
    assert "is not version 2" in messages["legacy"]
    assert "is not version 2" in messages["future"]


def test_user_discovery_uses_configured_data_dir_and_ignores_legacy_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "custom-user-data"
    legacy_home = tmp_path / "legacy-home"
    monkeypatch.setenv("HOME", str(legacy_home))
    current = user_data / "profiles" / "personal" / "workflows" / "current"
    current.mkdir(parents=True)
    (current / "workflow.toml").write_text(
        'version = 2\nname = "current"\ndescription = "Current root."\n'
        '[[steps]]\nid = "done"\nkind = "message"\nmessage = "done"\n',
        encoding="utf-8",
    )
    legacy = legacy_home / ".config" / "ricky" / "workflows" / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "workflow.toml").write_text(
        'version = 2\nname = "legacy"\ndescription = "Old root."\n'
        '[[steps]]\nid = "done"\nkind = "message"\nmessage = "done"\n',
        encoding="utf-8",
    )
    settings = RickySettings(user_data_dir=str(user_data))

    registry = discover_workflows(
        settings=settings,
        profile_scope=ProfileScope.create("personal"),
        skill_names=set(),
        tool_registry=ToolRegistry([]),
    )

    assert registry.get("current") is not None
    assert registry.get("legacy") is None
    assert find_workflow_bundle(
        "current",
        settings=settings,
        profile_scope=ProfileScope.create("personal"),
    ) == (current.resolve())
    assert (
        find_workflow_bundle(
            "legacy",
            settings=settings,
            profile_scope=ProfileScope.create("personal"),
        )
        is None
    )


def test_find_workflow_bundle_requires_qualification_for_duplicate_profile_names(
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    settings = RickySettings(user_data_dir=str(user_data))
    bundles: dict[str, Path] = {}
    for profile in ("personal", "work"):
        bundle = user_data / "profiles" / profile / "workflows" / "triage"
        bundle.mkdir(parents=True)
        (bundle / "workflow.toml").write_text(
            'version = 2\nname = "triage"\ndescription = "Triage."\n',
            encoding="utf-8",
        )
        bundles[profile] = bundle.resolve()
    scope = ProfileScope.create("work", access_profiles=["personal"])

    with pytest.raises(
        ValueError,
        match=r"personal/triage.*work/triage",
    ):
        find_workflow_bundle(
            "triage",
            settings=settings,
            profile_scope=scope,
        )

    assert (
        find_workflow_bundle(
            "personal/triage",
            settings=settings,
            profile_scope=scope,
        )
        == bundles["personal"]
    )
    assert (
        find_workflow_bundle(
            "work/triage",
            settings=settings,
            profile_scope=scope,
        )
        == bundles["work"]
    )


def test_find_workflow_bundle_prefers_profile_owner_over_bundled(
    tmp_path: Path, bundled_root: Path
) -> None:
    user_data = tmp_path / "user-data"
    profile_bundle = user_data / "profiles" / "personal" / "workflows" / "triage"
    bundled_bundle = bundled_root / "workflows" / "triage"
    for bundle in (profile_bundle, bundled_bundle):
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "workflow.toml").write_text(
            'version = 2\nname = "triage"\ndescription = "Triage."\n',
            encoding="utf-8",
        )
    settings = RickySettings(user_data_dir=str(user_data))
    scope = ProfileScope.create("personal")

    # An unqualified name resolves to the profile owner, not the bundled copy.
    assert (
        find_workflow_bundle("triage", settings=settings, profile_scope=scope)
        == profile_bundle.resolve()
    )
    assert (
        find_workflow_bundle("personal/triage", settings=settings, profile_scope=scope)
        == profile_bundle.resolve()
    )
    # The shadowed bundled definition stays reachable by its qualified identity.
    assert (
        find_workflow_bundle("bundled/triage", settings=settings, profile_scope=scope)
        == bundled_bundle.resolve()
    )


def test_description_shows_roots_dependencies_outputs_and_fan_in() -> None:
    compiled = compile_workflow(_valid_graph(), tool_registry=_registry())
    assert compiled.graph is not None

    text = describe_workflow(compiled.graph)

    assert "version: 2" in text
    assert "roots: source, decide, investigate" in text
    assert "[merge] data" in text
    assert "needs: source, decide, investigate" in text
    assert "risk: read_only" in text
    assert "body roots: note" in text
