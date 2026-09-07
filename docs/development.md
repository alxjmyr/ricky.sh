# Develop Ricky

Read [AGENTS.md](../AGENTS.md) for required commands and change rules, then the
[architecture](../.designs/architecture.md) for ownership boundaries. This guide
helps you find the smallest place to make a change. User instructions start at
the [documentation index](README.md).

## Start from the behavior

Ricky aims to make model-directed work reliable and predictable. The model can
propose an action; code owns permission, dispatch, persistence, and recovery.
A successful model response alone cannot establish that an external action
succeeded or that repeating it is safe.

Choose an existing execution path before adding another abstraction:

| Behavior | Starting point | Tests |
|---|---|---|
| Ordinary tool-using conversation | `agent/loop.py`, `agent/context.py` | `test_agent_loop.py`, `test_context_accounting.py`, `test_context_compaction.py` |
| Runtime construction and cleanup | `runtime/composition.py` | `test_web_search_cli.py`, `test_browser_runtime_composition.py` |
| Tool arguments, results, permissions | `tools/registry.py`, `agent/tool_dispatch.py` | `test_tool_authoring_contract.py`, `test_strict_tool_contracts.py`, `test_fresh_review_permissions.py` |
| Repeatable typed sequence | `workflows/compile.py`, `agent/workflow.py` | `test_workflows_compile.py`, `test_workflows_scheduler.py`, `test_workflows_effects.py` |
| Bounded unattended work | `jobs/runner.py`, `executions/dispatcher.py` | `test_jobs_runner_cli.py`, `test_execution_lifecycle.py` |
| Persistent conversation and delivery | `sessions/service.py`, `gateway/conversations.py`, `messaging/runtime.py` | `test_sessions_service.py`, `test_gateway_conversations.py`, `test_messaging_runtime.py` |
| Durable knowledge | `memory/store.py`, `memory/tools.py` | `test_memory_store.py`, `test_memory_integration.py` |
| Configuration and profile access | `config.py`, `profiles/` | `test_config.py`, `test_profiles_types.py`, `test_profile_management.py` |

Source paths in the table are relative to `src/ricky/`; test paths are relative
to `tests/`. Read the applicable subsystem design before changing its contract.
Tool templates and the reusable contract harness are linked from
[tool-authoring.md](../.designs/tool-authoring.md).

A skill supplies reusable instructions. A workflow enforces a sequence. A job
owns an unattended launch and budget. A schedule triggers a job. A durable task
tracks a responsibility; an execution request tracks an attempt under a pinned
contract. These objects solve different problems. Sharing their persistence or
lifecycle requires more than similar field names.

## Change CLI behavior at its owner

`interfaces/cli/app.py` assembles the command tree and retains the installation
gate, configuration commands, and chat/ask entry points. Other command groups
live in focused modules:

| Commands | Module under `interfaces/cli/` |
|---|---|
| `task` | `tasks.py` |
| `notification` | `notifications.py` |
| `execution`, `authority` | `executions.py` |
| `job`, `schedule` | `jobs.py` |
| `workflow` | `workflows.py` |
| `session`, `gateway`, `capability`, `browser`, `protected-values` | `sessions.py`, `gateway.py`, `capabilities.py`, `browser.py`, `protected_values.py` |
| Installation lifecycle and profiles | `installation.py`, `profiles.py` |

Follow the existing `register_*_commands()` pattern. Command modules import
their services directly; the application imports and registers commands.
`tests/test_architecture_boundaries.py` checks the direction of these imports
and the existing prohibition on core packages importing interfaces.

Patch test doubles where the command now looks up the dependency. For example,
workflow CLI tests patch `ricky.interfaces.cli.workflows.WorkflowRunner`.
Prefer fake providers, clients, and stores already used by the relevant test
file. The shared fixtures isolate live user data and bundled resources, and
refuse access to the real crontab.

Workflow CLI commands retain the existing `CapabilityRuntime` and reuse its
validated tool registry for compilation and execution. Extend that runtime
instead of reconstructing its tools or passing positional bundles of its fields.

## Remove complexity with evidence

Before deleting code, check source, tests, exports, command registration,
serialized formats, and documentation. An unreferenced internal helper is a
deletion candidate. An old serialized format or migration can still serve
installed data even when ordinary callers do not name it.

Prefer removing unused models, wrappers, and duplicate representations before
introducing a generic framework. Keep subsystem-specific transactions, effect
receipts, scope enforcement, and cancellation ownership explicit. Similar SQL
or exception handling can encode different recovery guarantees.

Use the focused test command from AGENTS.md while iterating. Complete changes
with `uv run pytest && uv run ruff check . && uv run pyright`; the full suite
includes real Chromium integration. A source move should preserve command
options, output, exit codes, and test assertions, as well as resource cleanup.

## Maintain the bundled user docs

Edit `docs/`, `ricky.toml.example`, and `.secrets.toml.example` as the canonical
sources. Do not edit generated files below
`src/ricky/builtins/skills/ricky-docs/references/`.

The Hatch build hook runs `scripts/bundle_docs.py` when building a wheel,
including an editable installation. It bundles user guides and examples, checks
local file links, and generates a versioned section index. The contributor guide
stays in the repository. No runtime writes or network fetches refresh these files.

After editing documentation in an existing editable checkout, refresh and check it:

```bash
uv run python scripts/bundle_docs.py
uv run python scripts/bundle_docs.py --check
```

The documentation tests check freshness, CLI examples, and job examples. Review
behavioral claims against their code owner when changing features; matching package
bytes cannot prove that a guide describes the behavior correctly.

Build and verify the actual release wheel with:

```bash
uv build --wheel --out-dir dist
uv run python scripts/bundle_docs.py --wheel dist/ricky-X.Y.Z-py3-none-any.whl
```

Replace `X.Y.Z` with the project version. The release action runs this verification
before publishing. Missing, extra, or stale references fail verification. A source
distribution includes the canonical inputs and hook so rebuilding it also bundles
the matching documentation.

## Improvement needs outcome evidence

Memory, editable profile skills, and workflow fixtures provide ways to retain
lessons and repeat successful procedures. They do not establish that a new
memory or instruction improves future results. Ricky currently has no general
automatic evaluation-and-promotion loop for such changes.

For a concrete improvement, capture the failure, define an observable expected
outcome, make the smallest change at its owner, and rerun the relevant examples
and regression tests. Keep the lesson in the appropriate profile or the
repository's current code and documentation. A new automatic learning or
promotion subsystem needs its own approved contract; do not infer success from
the model's assessment of its own answer.
