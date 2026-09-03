---
name: author-job
description: Interview the user about repeatable unattended work, create a strict version 3 project- or profile-owned job bundle, validate it with `ricky job validate` until clean, dry-run it, and show the resolved profile scope and authority for approval. Use when the user asks to create a Ricky job, make an ad-hoc run repeatable, or prepare work for a schedule.
---

# Author a job bundle

Create only version 3 job bundles. Every bundle must declare `version = 3`.
A job bundle is a standing grant of unattended authority. Keep it exact,
bounded, and readable.

## Choose the shape first

Do not create a bundle for work that runs one time. Use one ad-hoc command
instead:

```
uv run ricky job once "<goal>" --tool read_file --tool grep_search
```

An ad-hoc run is read-only, holds no job name, takes no lock, keeps no cursor,
and creates no recurrence state. Create a bundle only when the work repeats,
needs continuity between runs, needs standing mutation, or needs a schedule.

## Procedure

1. Interview the user before you write files. Identify:
   - the exact repeated outcome and the report the user wants to read;
   - the execution mode: an open-ended agent goal or one named workflow;
   - the inputs: locked workflow arguments, local files, Slack channels,
     Google accounts, or durable-task eligibility;
   - whether each run must remember where the last run stopped;
   - what prior conclusions future runs should inherit, and when the user would
     want a fresh context lineage;
   - the reads the job performs;
   - the mutations, if any, that the job may perform with no user present;
   - the work the job must escalate to the user instead of performing;
   - the bounds: wall clock, iterations, tokens, and external effect calls;
   - whether Ricky should automatically deliver every terminal job result or
     keep results available only through job history and reports;
   - whether a read-oriented headless browser is required, and if so whether it
     is a fresh public-HTTPS session or one exact authenticated resource with
     exact allowed origins and optional masked-image disclosure;
   - the trigger: manual only, or a cron schedule;
   - the owning profile and whether the bundle belongs to this project or to
     that profile across projects;
   - every additional profile whose data or resources a run must access.
2. State the job in plain language. Show its goal, inputs, tool list, standing
   mutations, budget, and escalation path. Get the user's agreement before you
   write files.
3. Choose a stable name that matches `^[a-z0-9][a-z0-9_-]{0,63}$`. The name is
   the ongoing responsibility and the identity of the lock, operational
   history, effect ledger, cursors, and every schedule. Keep that identity as
   the responsibility evolves; use context lineage rather than a rename when
   only prior model conclusions should start fresh.
4. Confirm that the current session's pinned profile scope contains the owner
   and every profile the authoring work must inspect. If it does not, stop and
   tell the user to start an appropriately scoped session; a skill cannot widen
   its runtime scope. For a project job, use `.ricky/jobs/<name>` and explain
   that the invocation's primary profile owns the resource. For a reusable
   profile job, run `uv run ricky config`, read `user_data_path`, and use
   `<resolved-user-data-dir>/profiles/<owner>/jobs/<name>`. Use `shared` only
   when the job is intentionally available in every runtime. Never guess or
   hardcode the user data root.
5. Call `run_shell` with `mkdir -p -- '<bundle-path>'` and confirm that the
   command succeeds. Write `<bundle-path>/job.toml` with `write_file`. Write
   `INSTRUCTIONS.md` separately when the goal is long, and then remove the
   `goal` key.
6. Define `<scope-flags>` as `--profile <primary>` plus one
   `--access-profile <name>` for each additional required profile. Validate
   after every draft with `run_shell`: `uv run ricky job validate
   <owner>/<name> <scope-flags>`. Fix every reported error and validate again.
   Stop only when the command reports `valid and available`.
7. Show the resolved job with `uv run ricky job show <owner>/<name>
   <scope-flags>`. Explain the owner and complete profile scope, the
   resolved provider and model, the agent tool list or workflow target and
   locked arguments, the standing mutations, the budget, and each declared
   source. Show the automatic result-notification policy separately from any
   explicit notification tool or workflow step.
8. Prove behavior with a dry run: `uv run ricky job run <owner>/<name>
   --dry-run <scope-flags>`. A
   dry run reasons and reads, and mechanically disables leases, mutations,
   effect reservations, escalation, and cursor commits. Ask the user before a
   dry run that reads external user data. Ask separately before the first live
   run.
9. Add a schedule only after the user approves the live behavior:
   `uv run ricky schedule add <owner>/<name> --cron "<expression>"
   <scope-flags>`, then `uv run ricky schedule sync <scope-flags>`. Explain
   that the schedule pins the profile scope and current
   exact spec and runtime-policy revisions plus a separate authority envelope.
   For jobs with Gmail or Calendar tools, that envelope snapshots the currently
   issued qualified account ids and expected email identities; it does not add
   an account allowlist to `job.toml`.
   A later edit needs validation and `uv run ricky schedule refresh <id>`;
   authority, trust-boundary, or timing expansion instead needs
   `uv run ricky schedule approve <id>`. Run `schedule sync` afterward.

## Authoring rules

### Bundle and identity

- Start with `version = 3`, `name`, and `description`. The spec is strict:
  an unknown key is an error.
- Keep `name` equal to the bundle directory name.
- Do not add a `profile` key to `job.toml`. The bundle location establishes a
  profile job's owner; the invocation establishes a project job's owner and
  the complete runtime scope.
- Keep `description` short, non-empty, and no longer than 500 characters. It
  goes into the run's system context.
- Set `provider` and `model` only when the job needs a specific one. Omit both
  to use the configured default selection.
- Author `[context]` with `lineage = 1` and `revision = 1`. These values are
  model-context controls, not job identity or retention settings.
- Prefer a project bundle for project-authored behavior. Prefer a profile
  bundle when the job should follow its owner across projects. Resources are
  identified as `<profile>/<name>`; use the qualified form in commands and
  schedules. Treat an unqualified name as shorthand only when discovery proves
  that it resolves to exactly one accessible job.

### Goal and instructions

- Declare exactly one of `goal` or `INSTRUCTIONS.md`. Both, or neither, is an
  error.
- Write the goal as the request for one run, not as a description of the job.
  State the required report and the decision boundary.
- Keep the instruction text under 50000 characters.
- Never place a secret, token, or personal identifier in the bundle. The
  bundle is committed content and is snapshotted under its digest.

### Workflow-backed jobs

- Add `[workflow] name = "<workflow>"` when the job must execute an authored
  workflow. Keep the goal: it tells the isolated resolver how to populate
  omitted invocation arguments and describes the intended run.
- Put stable values under `[workflow.args]`. Ricky treats them as locked and
  never asks the model to replace them. Omit only values that should be
  resolved from the goal, accessible profile resources, workflow argument
  descriptions, or prior successful-run metadata.
- Do not add `[tools].allow` or recurring sources to a workflow-backed job.
  Ricky compiles the workflow and derives its exact tool set.
- Do not use a workflow containing an `approval` step. Drafting output or
  calling `park_for_review` is compatible with unattended use because it does
  not wait for an inline response.
- List every mutating or destructive workflow tool in
  `[permissions].allow_mutating`. Validation rejects tools whose own contract
  forbids unattended use.
- A workflow identity or executable-bundle edit changes the job's context
  definition. Increment `context.revision` to preserve prior conclusions, or
  increment `context.lineage` and reset its revision to `1` to start fresh.
  The edit always needs schedule validation and refresh. It needs reapproval
  only when the compiled tool/data/effect authority expands.

### Budget

- Declare `[budget]` for every job. Every field is a hard bound for one run.
- Set `wall_clock_seconds` to the real expected duration plus margin.
- Set `iterations` to the number of tool-and-model turns the work needs. A run
  that exceeds it ends with `budget_exceeded`, not with a partial retry.
- Keep `effect_calls = 0` unless the job performs external mutations. The
  value is the exact maximum of reserved external actions for one run.

### Agent tools and standing authority

- List exact tool names in `[tools].allow`. There is no pattern, group, or
  wildcard. Choose only names you can see in your own tool list, then let
  `ricky job validate` prove availability on this machine.
- Keep the list minimal. Every extra tool widens unattended authority.
- A read-only tool needs no further declaration.
- For an agent job, a mutating or destructive tool must also appear in
  `[permissions].allow_mutating`.
  `allow_mutating` must be a subset of `tools.allow`.
- Risk class is not an unattended prohibition. A destructive tool is allowed
  when its tool contract declares unattended use as allowed and the job grants
  its exact name. A tool marked unavailable unattended is always forbidden.
- An external mutation must declare a deterministic effect identity, so that
  the harness can reserve it in the ledger and return a `performed`,
  `not_performed`, or `in_doubt` receipt. `park_for_review` is the safe default
  when the user must decide.
- Do not list `record_item_disposition` or `record_candidate_disposition`. The
  harness injects them when the job declares sources; naming them fails
  validation.
- Prefer escalation over authority. A job that parks a review task needs a much
  smaller grant than a job that acts.

### Read-oriented browser jobs

- Browser access is optional and read-oriented for named jobs. Do not grant
  clicks, fills, uploads, downloads, protected values, commits, CDP attachment,
  or user handoff. Browser tools remain unavailable to workflow-backed jobs.
- Add `[browser]` whenever `tools.allow` contains a browser tool. For a fresh
  headless session, set `allow_public_https_research = true` and include
  `browser_session_open`. For a persistent signed-in session, set
  `resource = "<profile>/<resource>"`, list every exact HTTPS origin in
  `allowed_origins`, and include `browser_session_open_resource` instead.
- Set `allow_masked_visual_observations = true` only when the job needs
  `browser_visual_snapshot`. The resource owner's profile must independently
  allow the pinned provider, and the selected model must accept images.
- A persistent resource must be Ricky-owned, configured `headless = true`, and
  inside the job's profile scope. Opening it is standing authenticated-data
  disclosure, so include `browser_session_open_resource` in
  `[permissions].allow_mutating` even though later observations are read-only.
- Browser settings, tools, resource configuration digest, origins, provider
  disclosure, and budgets are part of schedule authority. Widening them
  requires reapproval.

### Sources and continuity

- Declare a source only when a run must know what is new. A job that reads the
  same files every run needs no source.
- Use `[[stream_sources]]` with `adapter = "slack_channel"` for a channel
  stream. The adapter owns the cursor. Set `initial_lookback_hours` for the
  first run and `item_limit` for one batch.
- Use `[[task_sources]]` for durable-task eligibility. A task source is a
  query, never an assignment. Filter with `tags_any`, `tags_all`, `tags_none`,
  `execution_modes`, `statuses`, `waiting_on`, `due_before`, and `text`. Give
  `due_before` an explicit timezone offset; Ricky rejects ambiguous local
  datetimes and normalizes offset-aware values to UTC.
- Treat tags as open eligibility keys in the form `key:value`. Revision,
  execution mode, and the fenced lease remain the authority.
- Set `reconsider_after_hours` to control fairness. A candidate the job already
  accounted for returns only after that interval.
- Keep every source `name` unique inside its own list.
- The run must give every persisted item and candidate one terminal
  disposition. Say so in the goal. A stream cursor advances only after the
  batch is fully accounted for.
- Use `blocked` for work that needs the user. The harness escalates it into one
  correlated joint durable task that waits on the user.

### Reliability and effects

- Omit `result_notification` to keep the default `"always"` behavior. Set the
  top-level field to `"never"` only when the user wants to suppress Ricky's
  automatic completed, failed, or approval-required result notification. This
  does not suppress explicit `notify_user` calls or workflow notification
  steps, and it never removes job history, reports, transcripts, or effects.
- Assume every run starts with a fresh session and no prior transcript. Durable
  state is the only handoff: cursors, dispositions, durable tasks, and bounded
  prior run summaries.
- Live and dry-run summaries are separate lanes. A dry run never becomes live
  prior context or live workflow prior-success metadata.
- Expect one run of a name at a time. A second launch ends `skipped_locked`.
- Never plan a retry around an external effect. The harness reserves the action
  first and reports ambiguity as `in_doubt`; only the user reconciles it with
  `uv run ricky job action resolve`.
- Expect every changed job or workflow revision to require validation. Model,
  goal wording, and authority reductions can be refreshed without reapproval.
  Provider changes, new or changed tool contracts, new private-data scope,
  added standing mutations, increased effect ceilings, changed locked workflow
  arguments, and schedule timing changes require explicit approval.

## Minimal read-only skeleton

```toml
version = 3
name = "example-report"
description = "Report the current state of the project without changing it."
goal = """
Inspect the project and report its current status, risks, and next steps.
Read only. Do not claim to change any state.
"""

[context]
lineage = 1
revision = 1

[budget]
wall_clock_seconds = 300
iterations = 8
max_completion_tokens_per_request = 4096
effect_calls = 0

[tools]
allow = ["read_file", "list_dir", "glob_search", "grep_search"]
```

## Recurring skeleton with sources and standing authority

```toml
version = 3
name = "example-triage"
description = "Triage new channel messages and open task work into parked reviews."
goal = """
Account for every persisted item and candidate with exactly one disposition.
Park a review task for work that needs a user decision. Do not send messages.
"""

[context]
lineage = 1
revision = 1

[budget]
wall_clock_seconds = 600
iterations = 20
max_completion_tokens_per_request = 4096
effect_calls = 5

[tools]
allow = [
  "read_file",
  "grep_search",
  "slack_read_messages",
  "slack_read_thread",
  "read_durable_task",
  "claim_durable_task",
  "update_durable_task_progress",
  "release_durable_task",
  "park_for_review",
]

[permissions]
allow_mutating = [
  "claim_durable_task",
  "update_durable_task_progress",
  "release_durable_task",
  "park_for_review",
]

[[stream_sources]]
name = "eng"
adapter = "slack_channel"
channel_id = "C0123456789"
initial_lookback_hours = 24
item_limit = 50

[[task_sources]]
name = "open-triage"
tags_any = ["kind:triage"]
tags_none = ["review:skip"]
execution_modes = ["joint"]
statuses = ["open", "waiting"]
waiting_on = ["agent"]
limit = 10
reconsider_after_hours = 24
```

Read the stream cursor, the persisted batches, and the disposition history as
harness-owned state. The job never writes them directly; it records exact
dispositions and the harness advances continuity.

## Workflow-backed skeleton

```toml
version = 3
name = "workflow-report"
description = "Run a reviewed workflow with contextual invocation arguments."
goal = """
Run the supplied workflow for items created since the last successful run.
Return the workflow's terminal report.
"""

[context]
lineage = 1
revision = 1

[budget]
wall_clock_seconds = 600
iterations = 3
max_completion_tokens_per_request = 4096
effect_calls = 10

[workflow]
name = "reviewed-workflow"

[workflow.args]
account = "personal/personal"

[permissions]
allow_mutating = ["park_for_review"]
```
