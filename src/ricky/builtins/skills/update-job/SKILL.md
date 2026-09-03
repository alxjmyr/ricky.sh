---
name: update-job
description: Inspect and safely revise an existing Ricky job bundle while preserving its durable responsibility, classifying execution-revision, context-lineage, cursor, and schedule-authority consequences, validating with `ricky job validate`, and dry-running changed paths without dispatching effects. Use when the user asks to change a job's goal, budget, tools, standing mutations, sources, model, workflow, or to fix a defect in an existing job.
---

# Update an existing job bundle

Modify an existing `version = 3` job. Preserve its current authority and
behavior unless the user explicitly requests a change.

Do not create a new job with this skill. If no bundle exists, or the bundle
does not declare `version = 3`, stop and use `author-job`.

## Procedure

### 1. Establish the baseline

1. Establish the intended primary profile and every additional profile the
   work must access. Confirm that the current session's pinned scope contains
   them; a skill cannot widen its runtime scope. Define `<scope-flags>` as
   `--profile <primary>` plus one `--access-profile <name>` for each additional
   profile. Run `uv run ricky job list <scope-flags>` and resolve the exact
   qualified resource, such as `work/daily-brief`. Then run `uv run ricky job
   show <owner>/<name> <scope-flags>`. Do not guess when an unqualified name is
   ambiguous. Note whether the bundle is project-authored under `.ricky/jobs/`
   and therefore owned by the primary profile, or profile-owned under
   `<user_data_dir>/profiles/<owner>/jobs/`. Run `uv run ricky config` and read
   `user_data_path`; never guess or hardcode it.
2. Read `job.toml` and `INSTRUCTIONS.md` when it exists.
3. Run `uv run ricky job validate <owner>/<name> <scope-flags>` before
   editing. Record existing errors separately from the requested change. Do not
   report a pre-existing error as a regression.
4. Record the baseline:
   - resolved provider and model;
   - the exact agent `tools.allow` list, or workflow target and locked args;
   - the optional `[browser]` launch, resource, exact-origin, and masked-image
     disclosure ceiling, and whether the resource remains headless;
   - the exact `permissions.allow_mutating` list;
   - the budget, including `effect_calls`;
   - every stream source, its channel, and its cursor state;
   - every task source, its filters, its limit, and `reconsider_after_hours`;
   - the goal text and the escalation boundary it states.
   - `result_notification`, which defaults to `"always"` when omitted, and
     every explicit notification tool or workflow step separately;
   - the current `context.lineage` and `context.revision` (both default to `1`
     when omitted from an older bundle).
5. Read the recent history with `uv run ricky job history --job <owner>/<name>
   <scope-flags>` and `uv run ricky job report <run_id> <scope-flags>` for the
   last run. Check `uv run ricky job action show <action_id> <scope-flags>`
   when the job performs effects.
6. Resolve any `in_doubt` action before you change effect behavior. Only the
   user reconciles it, with `uv run ricky job action resolve`. Never change the
   bundle to hide an unresolved external action.
7. List every schedule that references the job with
   `uv run ricky schedule list`. Record its current state.

### 2. Define the requested change

Translate the request into observable scenarios. For each scenario, state:

- the input batch or candidate set;
- the required run behavior and disposition for each item;
- the required final report;
- the effects that may occur;
- the effects that must not occur;
- the behavior at the budget bound and after an interruption.

Classify each baseline behavior as:

- **change** — the user explicitly wants different behavior or authority;
- **preserve** — it must continue to work;
- **remove** — it is intentionally no longer supported.

Do not infer permission to add a tool, add a name to `allow_mutating`, raise
  `effect_calls`, widen a task-source filter, add a channel, change the provider,
or weaken the escalation boundary. These expand authority or cross a trust
boundary. A model change inside the same provider is an execution change but
does not itself expand authority.

### 3. Check the consequences before you edit

Classify the change by what it moves:

- **Profile ownership or scope** — moving a bundle between profile roots, or
  running a project bundle with a different primary profile, changes its
  qualified resource identity and data-access boundary. Changing additional
  accessible profiles changes what the run can discover and which provider
  policies apply. Treat either as an explicit authority and continuity change,
  not a routine file move. Prefer a new qualified job identity, preserve the
  old bundle and history until the user retires them, and review schedules.

- **Execution revision** — any byte of `job.toml`, `INSTRUCTIONS.md`, or a
  referenced executable workflow bundle changes the exact spec/runtime pins.
  Every referencing schedule stops launching until the revision is validated.
  A revision that stays inside the existing approval envelope is accepted with
  `uv run ricky schedule refresh <id>`.
- **Approval envelope** — provider trust boundary, exact tool contracts,
  standing mutations, private-data source scopes, locked workflow arguments,
  the external-effect ceiling, and the qualified Google account identities
  issued to jobs with Gmail or Calendar tools. New or wider authority requires
  explicit `uv run ricky schedule approve <id>`. A newly available account or
  changed email identity requires approval; account removal and other
  tool/source/budget reductions remain within the prior envelope and need only
  refresh. Any schedule timing edit is explicitly approved; `schedule sync`
  never grants authority.
- **Browser authority** — adding browser access, switching between ephemeral
  and persistent launch, changing the qualified resource or its configuration,
  adding an allowed origin, enabling masked visual observations, or adding a
  browser tool changes the exact disclosure envelope and requires schedule
  reapproval. Named jobs remain read-oriented: reject clicks, fills, uploads,
  downloads, protected values, commits, CDP attachment, user handoff, and all
  browser tools in workflow-backed jobs.
- **Context definition** — the job goal, workflow identity, or referenced
  executable workflow bundle. Before editing, ask the user or an authorized
  job-owning agent whether to preserve prior conclusions or start fresh.
  Preserve them by incrementing `context.revision` and keeping
  `context.lineage`; start fresh by incrementing `context.lineage` and resetting
  `context.revision = 1`. Never rename the job or delete history for this.
- **Continuity** — a stream source `name`, its `channel_id`, or its adapter.
  The cursor belongs to the source identity. A rename or a channel change
  starts a new cursor, and `initial_lookback_hours` decides the first batch
  again. Removing a source abandons its cursor and its unaccounted items.
- **Fairness** — `reconsider_after_hours`, a task-source filter, or a `limit`.
  A lower interval or a wider filter re-surfaces candidates the job already
  accounted for.
- **Bounds** — `wall_clock_seconds`, `iterations`,
  `max_completion_tokens_per_request`, or `effect_calls`. Raising a bound
  raises the maximum unattended cost of one run. Only `effect_calls` expands
  the approval envelope; the other bounds require validation and refresh.
- **Local** — description, model within the same provider, source batch limits,
  non-effect bounds, and `result_notification`. These still need validation
  but not reapproval. Changing `result_notification` controls only Ricky's
  automatic terminal projection; it does not alter explicit job or workflow
  notification effects, durable run evidence, or context lineage.

Runner-injected disposition tools are harness bookkeeping, not user-granted
authority. Their contract changes require validation and refresh, never user
approval. If validation reports that one no longer satisfies its confined
Ricky-state invariants, stop; do not approve around the failure.

Show the user a compact before-and-after list when the change touches the
execution revision, effects, sources, or several keys. A direct, unambiguous request for a
local correction does not need a redundant approval.

### 4. Apply the smallest coherent change

Use `edit_file` for a narrow edit. Use `write_file` only when the complete file
must be replaced. Preserve unrelated user edits and the existing formatting.

Keep these invariants:

- Keep `version = 3` and keep `name` equal to the bundle directory name. Do not
  rename a job; the name is its ongoing responsibility and the identity of its
  lock, operational history, effects, cursors, and schedules.
- Keep positive `[context] lineage` and `revision` values. If an older bundle
  omits the table, treat it as lineage `1`, revision `1`, and add the table when
  making a context-affecting change.
- Do not add a `profile` key to `job.toml`. Ownership comes from the profile
  bundle path, or from the primary profile when the bundle is project-authored.
- Keep exactly one of `goal` and `INSTRUCTIONS.md`. When you move a long goal
  into `INSTRUCTIONS.md`, remove the `goal` key in the same change.
- Add no unknown key. The spec is strict.
- For an agent job, keep `allow_mutating` a subset of `tools.allow`. Removing a
  tool from `tools.allow` requires removing it from `allow_mutating` too.
- For a workflow job, keep `tools.allow` empty. Preserve locked arguments
  exactly unless the user requested a change, and let validation derive the
  workflow tool set.
- Never add a tool whose contract forbids unattended use. Destructive tools are
  allowed only when their declaration explicitly permits unattended use and
  `allow_mutating` grants the exact tool.
- Never make a workflow job target a graph containing an `approval` step.
  Drafts and parked review tasks are allowed because they do not pause the run.
- Never add `record_item_disposition` or `record_candidate_disposition` to
  `tools.allow`. The harness injects them when sources exist.
- Keep `effect_calls` at the exact maximum of external actions one run may
  reserve. Keep it `0` while the job stays read-only.
- Keep `result_notification` omitted for its default `"always"` behavior, or
  set it to `"never"` when the user explicitly wants automatic terminal job
  results suppressed. Do not treat it as a switch for explicit notifications.
- Keep the accounting requirement in the goal when the job declares sources.
  Every persisted item and candidate needs exactly one terminal disposition.
- Keep escalation as the path for work that needs the user. Prefer
  `park_for_review` over new standing authority.
- Keep every source `name` unique inside its list.
- Never plan a retry around an external effect, and never weaken an effect
  identity to make replay look safe.
- Put no secret or personal identifier in the bundle.

### 5. Validate and inspect

1. Run `uv run ricky job validate <owner>/<name> <scope-flags>` after each
   coherent draft.
2. Fix every new error and validate again until it reports
   `valid and available`.
3. Compare `uv run ricky job show <owner>/<name> <scope-flags>` with the
   baseline. Confirm that only the agreed keys changed, especially the tool
   list, the standing mutations, the budget, and the sources.
4. Do not declare the job ready on valid TOML alone. Validation proves shape,
   current tool availability, and stream-adapter availability. It does not
   prove behavior.

### 6. Prove behavior without effects

1. Dry-run the job: `uv run ricky job run <owner>/<name> --dry-run
   <scope-flags>`. A dry run reasons
   and reads, and mechanically disables leases, mutations, effect
   reservations, escalation, cursor commits, and fairness writes. Ask the user
   before a dry run that reads external user data.
2. Cover at least:
   - each changed outcome or disposition rule;
   - each preserved path that touches the changed tools or sources;
   - an empty batch and a full batch at `item_limit` or `limit`;
   - the budget bound when a bound changed;
   - the escalation path when the job can block.
3. Read the resulting report with `uv run ricky job report <run_id>`. Confirm
   the dispositions, and confirm that no effect was reserved.
4. Ask separately before the first live run after an effect or authority
   change.
5. Remember that dry-run context is isolated from live context. A successful
   dry run cannot become the live workflow resolver's prior success.
6. If repository tests cover jobs or this bundle, add cases for the requested
   behavior. Do not change an existing expected behavior unless the user
   approved that contract change.

### 7. Reconcile schedules last

1. Run `uv run ricky schedule list`. A referencing schedule reports one exact
   gate: `lineage_required`, `validation_required`, or `approval_required`.
2. Resolve `lineage_required` in the job file and validate again. Do not let a
   schedule command choose context implicitly.
3. For `validation_required`, run `uv run ricky schedule refresh <id>`. Refresh
   fails closed if the current revision exceeds the stored approval envelope.
4. For `approval_required`, report exactly what expands or crosses a boundary.
   Run `uv run ricky schedule approve <id>` only with explicit user authority.
5. Run `uv run ricky schedule sync` after every successful refresh or approval,
   then verify the schedule reports `ready`.
6. Run `uv run ricky schedule doctor` when installed state may have drifted.
7. Never edit the crontab directly, and never rewrite desired state to make a
   changed job look approved.

### 8. Report the update

Report:

- the behavior that changed;
- the behavior verified as preserved;
- validation and dry-run evidence;
- tool, standing mutation, budget, or provider changes;
- execution-revision, approval-envelope, and context-lineage consequences;
- cursor and fairness consequences;
- each schedule that now needs approval;
- any live read or effect test that was not run.

Do not claim that an untested path passed.

## Scope boundaries

Use `author-job` to create a new ongoing responsibility. Do not split an
existing responsibility merely because its execution, authority, or context
changes; preserve its qualified job identity and use the revision gates. Use
this skill only after a version 3 bundle exists.

Diagnose a runtime failure before changing the bundle. If the bundle is valid
and the defect is in the runner, ledger, lock, source adapter, effect guard,
schedule adapter, or a registered tool, report that evidence and fix that
component only when the user asked for an implementation change. Do not
compensate for a harness defect by widening the job's authority.
