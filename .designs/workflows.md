# Workflows

This document defines the durable workflow boundaries. User-facing commands and
authoring examples belong in `docs/workflows.md`; the canonical authoring models
live in `ricky.workflows.spec`.

## Purpose and boundary

A workflow is a declarative, version-2 typed execution DAG for a repeated
process whose order, data handoffs, approvals, effects, or recovery state must
be enforced by the harness. Use a prompt skill or ordinary agent turn when the
sequence itself should remain an open-ended model judgment.

Workflows run beside the agent loop. The scheduler owns graph progress,
completion, parallelism, typed data flow, checkpoints, and terminal status.
`AgentLoop` does not interpret workflow graphs; an `AgentSession` carries only a
queued `WorkflowInvocation`, while `WorkflowRunStore` owns durable run state.
Workflow progress is emitted through the normal typed agent event stream.

Only `version = 2` is valid. Missing, legacy, and unknown versions fail
discovery. Do not introduce a compatibility runner or a second workflow
contract without an approved architectural change.

## Bundles and identity

A bundle contains `workflow.toml` plus optional passive instruction and
reference files. Ricky never imports Python or extension code from a bundle;
declared shell checks are the explicit trusted executable-content exception.

```text
<user_data_dir>/profiles/<owner>/workflows/<name>/workflow.toml
<installed package>/ricky/builtins/workflows/<name>/workflow.toml
```

The directory name must match the spec name. Profile bundles belong to their
directory's profile; bundled definitions carry the reserved `bundled` owner and
no profile. Runtime identity is the qualified `profile/name` or `bundled/name`;
an unqualified name is usable only when it resolves unambiguously inside the
issued profile scope. A profile definition takes precedence over a bundled
definition with the same bare name. See [builtins.md](builtins.md).

Instruction files are UTF-8, bundle-relative, confined beneath the bundle, and
bounded by typed workflow settings. Other bundle assets are passive inputs;
executable behavior enters only through registered tools and operators.

## Authoring contract

A spec declares typed invocation `args`, reusable result `schemas`, and at least
one step. The supported step kinds are:

| Kind | Contract |
|---|---|
| `model` | One isolated, tool-free model task returning a schema-validated JSON value. |
| `agent` | One isolated bounded model loop with only explicitly named read-only tools, returning a schema-validated JSON value. |
| `tool` | One registered tool call with typed arguments through the normal permission and effect path. |
| `data` | One registered pure operator with no I/O or session access. |
| `check` | A pure condition or bounded authored shell check. |
| `approval` | An explicit human confirmation or stable-key selection. |
| `message` | A deterministic user-facing string. |
| `foreach` | A bounded map of one nested acyclic graph over a collection. |

Steps declare dependencies with `needs`. `dependency_policy = "success"`
requires dependencies to complete or skip successfully; `"terminal"` permits a
step to inspect failed terminal branches. A pure `when` condition may skip a
ready step. `on_error` determines whether a failure stops the graph or permits
independent work to continue.

Values are JSON-safe literals, references, lists, objects, or restricted named
string formats. References may read declared trigger arguments, dependency step
records, and—inside `foreach`—the current item and dependency item records. The
compiler rejects unknown, non-upstream, or schema-invalid references. Runtime
resolution fails closed and materializes only the current step's declared
bindings; binding and result size ceilings do not require serializing unrelated
checkpoint state.

The top-level graph and each `foreach` body must be acyclic and reachable from
at least one root. Step ids are unique across graph scopes. Nested `foreach` is
not supported. Each item has a stable scalar key, retains source order in the
aggregate output, checkpoints independently, and may project a compact output
while the full item record remains durable.

Compilation validates the complete executable surface before a run exists:
graph integrity, arguments, references, result-schema depth, instruction files,
registered tools, prompt skills, pure operators, parallel and iteration limits,
tool parameter names, output contracts, and retry safety. The compiled
fingerprint includes the spec and relevant tool/operator contracts.

## Model context and data flow

`model` and `agent` steps receive a private request containing only the step
instruction, explicitly bound inputs, result schema, and an optional explicitly
named skill body. They do not inherit chat history, memory, profile/resource
catalogs, unrelated workflow records, or prior model transcripts.

A `model` step has no tools. An `agent` step can call only its compiled
read-only tool list through the normal registry and permission path. Both must
return one JSON value matching a Ricky-owned result schema. Private task
transcripts are not persisted. Validated outputs, bounded errors, and usage are
checkpointed; context metrics are emitted as typed debug events.

Tool steps consume `ToolResult.data` through the tool's declared result model;
display text is not a data interface. Set `expose_output = false` when later
steps do not need the returned data. Deterministic transformations belong in
registered data operators rather than model prose or arbitrary bundle code.

## Scheduling, effects, and recovery

The scheduler selects ready steps in document order and runs independent safe
work within configured parallelism. Approval steps and non-read-only tool steps
are exclusive. Cancellation cancels and awaits owned tasks, marks interrupted
records, and checkpoints before returning control.

Every tool call uses the shared argument normalization, permission, dispatch,
and event path. An approval step records human intent but never pre-authorizes a
later tool call. Authored shell checks are trusted local executable content: they
run as bounded subprocesses in the workflow working directory. The authoring
contract requires them to be side-effect free, but the harness cannot prove
that property, so they require the same review discipline as a local script.

Effectful tool steps write journal evidence before and after dispatch. A
dispatched effect without a known result becomes `in_doubt`; Ricky must not
replay it until a user reconciles observed external truth. A performed receipt
is a permanent no-replay boundary. A known failed or `not_performed` effect is
retryable only when the compiled tool contract declares safe, idempotent replay
with a stable key and the step has an applicable remaining retry.

Dry-run validates and journals effectful calls as `would_dispatch` without
permission prompts or dispatch. It may still invoke models and real read-only
tools. Approval steps fail closed unless supplied by explicit fixtures.

Run checkpoints are atomic JSON-safe records below
`<user_data_dir>/<workflow.run_dir>`, regardless of whether the bundle is
project- or profile-authored. They retain source provenance, provider/model,
the complete issued `ProfileScope`, graph fingerprint, step and item records,
effect journal, usage, and bounded errors. Store APIs enforce profile scope.
Resume requires the exact profile scope and compiled fingerprint; changed graphs
do not resume in place.

## Jobs and unattended execution

A named job may invoke exactly one workflow and owns the outer unattended
envelope: launch identity, schedule approval, overlap exclusion, wall-clock and
effect budgets, standing mutation authority, notifications, and argument
resolution. The workflow remains authoritative for its graph, steps,
checkpoints, outputs, retry policy, and effect journal.

Workflow-backed jobs reject inline `approval` steps. A mutating or destructive
tool is eligible unattended only when its own contract allows unattended use
and the job grants that exact tool. Workflow bundle changes participate in the
job's execution revision and context-lineage checks; job and workflow records
remain linked rather than copied into one lifecycle.

An installation upgrade may deterministically migrate a profile-owned job's
representation only when the user requests job updates. Project-owned jobs are
validated against the target runtime but are never rewritten. Upgrade uses the
ordinary schedule refresh and approval comparison: representation-only and
equal-or-narrower authority changes may preserve and resync an existing
schedule, while any material authority expansion requires the ordinary
explicit schedule approval. `--update-jobs` is migration permission, never an
authority grant. A job that is invalid, unavailable, or awaiting reapproval
keeps its desired schedule record but is omitted from the installed cron block
until it is safe to run.

## Change checklist

When extending workflows, preserve these properties:

1. Control flow and completion remain scheduler-owned, never model-reported.
2. All data crosses steps through validated JSON-safe references and outputs.
3. Model context stays isolated and agent-step tools remain read-only.
4. Mutations occur only in explicit tool steps through normal permissions and
   effect journaling.
5. Retry never crosses an observable or ambiguous effect boundary.
6. Every owned async task or subprocess is cancelled, awaited, and checkpointed
   into a valid recoverable state.
7. Persisted runs retain exact graph and profile-scope evidence.
