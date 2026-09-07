---
name: update-workflow
description: Inspect and safely revise an existing Ricky Workflow V2 bundle while preserving unrelated behavior, checking graph and saved-run compatibility, validating the changed DAG, and testing changed and unchanged paths without dispatching effects. Use when the user asks to add, remove, reorder, or replace workflow steps; change goals, outcomes, arguments, schemas, conditions, approvals, effects, retry behavior, success criteria, or foreach processing; or fix a defect in an existing V2 workflow.
---

# Update an existing Workflow V2 execution graph

Modify an existing `version = 2` workflow. Preserve its current contract and
behavior unless the user explicitly requests a change.

Do not create a new workflow with this skill. Do not edit a V1 workflow. If the
bundle has no `version = 2`, stop and tell the user to convert it with
`author-workflow` before applying behavioral changes.

Use installed `ricky` commands. In a development checkout, follow its contributor
instructions and use `uv run ricky` (for example, `uv run ricky config`).
All user bundles belong below the resolved profile root; projects are not
discovery roots. Bundled resources are read-only. To customize one, copy its
complete bundle into an accessible profile and use that qualified identity.

## Procedure

### 1. Establish the baseline

1. Establish the intended primary profile and every additional profile the
   work must access. Confirm that the current session's pinned scope contains
   them; a skill cannot widen its runtime scope. Define `<scope-flags>` as
   `--profile <primary>` plus one `--access-profile <name>` for each additional
   profile. Run `ricky workflow list <scope-flags>` and resolve the
   exact qualified resource, such as `work/inbox-triage`. Locate the profile-owned bundle under
   `<user_data_dir>/profiles/<owner>/workflows/<name>/`. Run `ricky config`
   and read `user_data_path` rather than guessing it. For a bundled source,
   establish the profile copy and review referencing jobs before proceeding.
   Do not guess when an unqualified name is ambiguous.
2. Read `workflow.toml` and every instruction file that it references. Read
   passive bundle resources only when the workflow or requested change uses
   them.
3. Call `validate_workflow` with `<owner>/<name>` before editing; its session
   scope must match `<scope-flags>`. Also run `ricky workflow validate
   <owner>/<name> <scope-flags>`. Record existing validation errors separately
   from the requested change. Do not represent a pre-existing error as a
   regression.
4. Record the compiled graph:
   - typed invocation arguments;
   - roots, dependencies, conditions, and fan-in;
   - model and agent inputs and result schemas;
   - `foreach` sources, stable keys, body steps, and output projections;
   - approval and effect boundaries;
   - terminal messages or outputs;
   - retry and failure policies.
5. Ask whether any incomplete saved run must remain resumable when that is not
   already clear. A changed graph fingerprint cannot resume automatically.
   Finish or abandon important incomplete runs before editing. Never rewrite a
   checkpoint to make it appear compatible.
6. Find every named job whose `[workflow].name` resolves to this workflow. Read
   those jobs' `context.lineage` and `context.revision`, then list their
   schedules and current states. A workflow bundle edit changes each referencing
   job's context definition and exact execution revision even when its tool
   authority is unchanged.

### 2. Define the requested change

Translate the request into observable scenarios. For each scenario, state:

- the trigger arguments and starting data;
- the required step or branch behavior;
- the required final result;
- the effects that may occur;
- the effects that must not occur;
- the failure and interruption result.

Classify each baseline behavior as:

- **change** — the user explicitly wants a different contract or outcome;
- **preserve** — it must continue to work;
- **remove** — it is intentionally no longer supported.

Do not infer permission to rename or remove invocation arguments, change an
effect target, weaken approval, broaden tool access, or discard a supported
outcome.

### 3. Design the graph delta

Before writing, show the user a compact before-and-after graph when the change
alters goals, public arguments, outcomes, effects, or several steps. Identify:

- added, changed, and removed nodes;
- added, changed, and removed dependencies;
- every downstream reference affected by an output or schema change;
- condition and dependency-policy changes;
- `foreach` projection changes;
- approval, permission, retry, and idempotency changes;
- saved-run compatibility.

Get agreement before applying a material contract or effect change. A direct,
unambiguous request for a local correction does not require a redundant
approval.

Moving a workflow between profile roots, or invoking a project workflow with a
different primary profile, changes its qualified identity and data-access
boundary. Changing additional accessible profiles changes discovery and
provider policy. Treat either as an explicit ownership, authority, and saved-run
compatibility change—not as a routine file move. Prefer a new qualified
workflow identity and preserve important incomplete runs under their original
scope until they finish or are abandoned.

For each referencing job, ask whether the changed workflow should preserve its
prior conclusions or start fresh. Preserve them by incrementing the job's
`context.revision`; start fresh by incrementing `context.lineage` and resetting
its revision to `1`. Do not rename the job or delete operational history. A
compiled tool addition/change, new standing mutation, wider private-data scope,
changed locked arguments, provider boundary change, or increased effect ceiling
also requires schedule reapproval. A workflow implementation change that stays
inside the approved envelope needs validation and `schedule refresh`, not
reapproval.

### 4. Apply the smallest coherent change

Use `edit_file` for narrow edits. Use `write_file` only when a complete file
must be replaced. Preserve unrelated user changes and bundle formatting.

Keep these V2 invariants:

- Keep `version = 2`. Do not introduce V1 `entry`, transition tables,
  `prompt`, `router`, `complete_step`, captures, or binding templates.
- Do not add a `profile` key to `workflow.toml`. Ownership comes from the
  profile bundle path, or from the primary profile for a project bundle.
- Keep the top-level graph and each `foreach` body acyclic. Declare all
  dependencies with `needs`.
- Keep invocation arguments typed and documented. Treat a rename, removal,
  type change, default change, or constraint tightening as a public contract
  change.
- For profile-owned resource arguments, verify that descriptions require the
  exact profile-qualified id from the accessible resource catalog. Remove
  stale unqualified defaults such as `personal` or `work`; a multi-profile
  project/shared workflow should normally require the caller to select the
  resource explicitly.
- Use typed `{ ref = "..." }` expressions. Update every consumer when an
  output path or result schema changes.
- Give each `model` or `agent` step only the declared inputs it requires.
  Keep object schemas closed and bound variable-length data where practical.
- Use `data` steps for deterministic project, filter, partition, sort, limit,
  and merge operations. Do not move deterministic control into a model prompt.
- Preserve stable `foreach` item keys. Keep a non-empty, compact `outputs`
  projection containing only downstream data. Do not project full read
  results, source records, attempts, or timestamps without a declared need.
- Apply `workflow.max_binding_chars` to the current step's resolved
  bindings. Reduce broad inputs or projections instead of increasing the
  limit to hide an oversized contract.
- Keep proposal, approval, and effect as separate steps. Mutating and
  destructive operations must remain explicit tool steps.
- Add effect retries only when replay is declared safe and the step has an
  idempotency key. Never hide an `in_doubt` effect with automatic replay.
- Use `dependency_policy = "terminal"` only for reporting or recovery that
  must inspect failed or skipped dependencies.
- Use `on_error = "continue"` and `on_item_error = "collect"` only when a
  later path handles or reports the failure.
- Update every referencing job's context values in the same coherent change.
  Leaving the old revision intentionally makes live and scheduled job preflight
  stop at `lineage_required`.

### 5. Validate and inspect

1. Call `validate_workflow` with `<owner>/<name>` after each coherent draft,
   and run `ricky workflow validate <owner>/<name> <scope-flags>`.
2. Fix all new errors and validate again until the result is `valid`.
3. Compare the compiled graph with the baseline. Confirm that only the agreed
   nodes, edges, conditions, schemas, projections, approvals, and effects
   changed.
4. Check every changed reference from producer to consumer. Pay special
   attention to conditional branches, terminal-policy fan-in, and paths inside
   later `foreach` items such as `item.source.output.<projection>`.
5. Do not declare the workflow ready based only on valid TOML or a successful
   compile.
6. Validate every referencing job. For each schedule, resolve
   `validation_required` with `schedule refresh`, resolve
   `approval_required` only with explicit authority, then run `schedule sync`
   and verify `ready`.

### 6. Prove behavior without effects

Build a regression matrix with at least:

- each changed outcome or success criterion;
- boundary values for changed arguments or conditions;
- each preserved branch that touches the changed subgraph;
- failure or collected-item behavior when it changed;
- approval denied or no-selection behavior when an effect is reachable.

Use fixture dry-runs when practical. Dry-run must not dispatch mutating or
destructive effects. Ask before a live dry-run that reads external user data.
Ask separately before any real effect test.

If repository tests cover the bundle or workflow framework, add new cases for
the requested behavior. Do not change an existing expected behavior unless the
user approved that contract change.

### 7. Report the update

Report:

- the behavior that changed;
- the behavior verified as preserved;
- validation and dry-run evidence;
- public argument or result changes;
- approval, effect, retry, or failure-policy changes;
- whether old incomplete checkpoints can resume;
- each referencing job's preserve/start-fresh lineage choice and schedule gate;
- any live read or effect test that was not run.

Do not claim that an untested branch passed.

## Scope boundaries

Use `author-workflow` to create a new workflow or convert V1 to V2. Use this
skill only after a V2 bundle exists.

Diagnose a runtime or framework failure before changing the bundle. If the
definition is valid and the defect is in the compiler, scheduler, checkpoint
store, provider adapter, or a registered tool, report that evidence and fix
the correct component only when the user requested an implementation change.
Do not compensate for a framework defect by weakening the workflow contract.
