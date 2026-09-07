# Run jobs and schedules

Jobs perform bounded work in fresh agent sessions. Schedules run named jobs through Ricky's managed
cron adapter.

## Choose a job type

Use `job once` for one read-only goal that does not need a reusable identity:

```bash
ricky job once \
  "Summarize the current project risks" \
  --tool read_file \
  --tool list_dir \
  --tool grep_search \
  --provider claude_code \
  --model sonnet
```

`job once` is strictly read-only. It has no named overlap lock, but it still creates a run record
and retained transcript.

Use a named job when work must be repeatable, inspectable, protected from overlap, or scheduled.
Named version 3 jobs support two execution modes:

- An agent job runs its goal in a bounded agent session with an exact `[tools].allow` list.
- A workflow job resolves omitted workflow arguments from its goal and context, then runs one named
  workflow through the workflow runner.

Both modes can use explicitly authorized Ricky-state mutations or guarded external effects. Risk
class does not determine unattended eligibility: a mutating or destructive tool can run when its
tool contract allows unattended use and the job lists it in `permissions.allow_mutating`.

A qualified job name represents one ongoing responsibility. Ricky keeps that identity—and its
locks, schedules, cursors, operational history, and effect ledger—when the responsibility evolves.
It tracks two kinds of change separately:

- The execution revision is the exact validated job and resolved runtime configuration.
- The context lineage selects which prior run conclusions may enter future model context.

Schedule approval is a third boundary. It records the maximum unattended authority and trust
boundary already approved for that schedule; it is not the job's identity or context history.

## Discover and inspect named jobs

Ricky discovers jobs in this order:

1. `<user_data_dir>/profiles/<name>/jobs/<job-name>/job.toml` for every accessible profile
2. The jobs bundled with Ricky

Resources use qualified names such as `work/project-brief`. An unqualified name is accepted only
when it resolves to one accessible job. A bundled job carries the reserved `bundled` owner and the
qualified name `bundled/<name>`. A job you author with the same bare name takes precedence over a
bundled job, which stays reachable as `bundled/<name>`. No named jobs are currently shipped;
create a profile job before using the named-job commands below.

### Create your first job

Find `user_data_dir` with `ricky config`. Create the directory
`<user_data_dir>/profiles/shared/jobs/project-brief/`, then save this as `job.toml`:

```toml
version = 3
name = "project-brief"
description = "Summarize the current project's status without changing files."
goal = "Read README.md and list the top-level files. Report the project purpose and next steps."
result_notification = "never"

[context]
lineage = 1
revision = 1

[budget]
wall_clock_seconds = 300
iterations = 8
max_completion_tokens_per_request = 4096
effect_calls = 0

[tools]
allow = ["read_file", "list_dir"]
```

The job uses the runtime's configured provider and model. Run the examples from a
project containing `README.md`, or pass `--project /absolute/path/to/project`.
Use another owning profile if this job should not be available everywhere.

```bash
ricky job list
ricky job list --profile work --access-profile personal
ricky job validate project-brief
ricky job show project-brief
```

Validate a job on every machine that will run it. Validation checks its shape and current tool
availability. For a workflow job, it also compiles the workflow, validates authored arguments,
derives the exact tool set, checks unattended authority, and rejects interactive approval steps.
If the goal or referenced workflow changed after a live run, validation also requires the authored
context revision to acknowledge whether prior conclusions remain compatible.

## Run and inspect a named job

```bash
ricky job run project-brief
ricky job history --job project-brief
ricky job report RUN_ID
```

Named jobs use a per-job lock. If the same job is already running, the overlapping launch records
`skipped_locked` instead of starting a second copy.

Dry-run a named job before unattended use:

```bash
ricky job run project-brief --dry-run
```

Dry run denies mutations, but it still performs model calls and reads, including allowed network
reads. It is not free of token cost or external data access.

Dry-run summaries and workflow resolver metadata stay in a separate context lane. They never
become prior context for a live run, and live conclusions are not injected into a dry run.

If a guarded external effect has an unknown result, inspect the external system before you resolve
it:

```bash
ricky job action show ACTION_ID
ricky job action resolve ACTION_ID --performed
ricky job action resolve ACTION_ID --not-performed
```

Ricky does not automatically repeat an in-doubt effect.

Job status follows durable evidence, not the model's final prose. Success requires a `performed`
receipt for each external action that remains part of the run. An abandoned denial or known
`not_performed` attempt fails the run; an unresolved or `in_doubt` attempt makes it uncertain. A
later performed call can supersede an earlier invalid proposal only when Ricky can correlate the
two as the same repaired action.

## Author a named job

Use the included authoring skill:

```text
/skill author-job
```

A job bundle contains `job.toml` and can contain an `INSTRUCTIONS.md`. The file must declare
`version = 3`, its name must match the directory, and it must define exactly one of an inline goal
or `INSTRUCTIONS.md`.

New jobs should declare their initial context coordinates:

```toml
[context]
lineage = 1
revision = 1
```

If the goal, workflow identity, or executable workflow bundle changes, choose one path:

- Preserve prior conclusions: keep `lineage` and increment `revision`.
- Start with fresh model context: increment `lineage` and reset `revision` to `1`.

Starting fresh does not delete or move runs, effects, cursors, transcripts, or audit history.
Changing provider, model, tools, budgets, sources, or locked arguments does not by itself change
context lineage.

Choose ownership before creating the bundle. Put jobs in
`<user_data_dir>/profiles/<profile>/jobs/<name>/`. Use `shared` only for jobs intended to be
available in every runtime. Profile ownership comes from the bundle location, not a `job.toml`
field.

Author, validate, and dry-run with the same `--profile` and `--access-profile` scope the live job
will use. Prefer the qualified `profile/name` resource reference in commands and schedules.

A job specification pins:

- Provider and model
- Goal or instructions
- Wall-clock, iteration, completion-token, and effect budgets
- An exact agent tool list, or one workflow and its locked arguments
- Exact standing authority for mutating and destructive tools
- Optional task or stream sources
- Result and notification behavior

## Give a named job read-oriented browser access

Named and scheduled jobs can own one exact headless browser for a run. They may open,
navigate, scroll, select pages, and take semantic or explicitly disclosed masked visual snapshots.
They cannot click or fill controls, upload or download files, use protected values, hand off to a
user, or commit a financial or generic browser transaction. Workflows still cannot contain browser
tools.

Enable the installation's `[browser.background]` read ceiling first. An ephemeral public-HTTPS
research job declares its launch and disclosure scope separately from its exact tools:

```toml
version = 3
name = "public-research"
description = "Research public HTTPS sources in a fresh headless browser."
provider = "openrouter"
model = "your-model"
goal = "Research the assigned subject and return linked findings."

[browser]
allow_public_https_research = true
allow_masked_visual_observations = false

[tools]
allow = [
  "browser_session_open",
  "browser_session_close",
  "browser_pages",
  "browser_page_select",
  "browser_navigate",
  "browser_scroll",
  "browser_snapshot",
]
```

For a signed-in persistent profile, configure a Ricky-owned resource with `headless = true`, then
pin that qualified resource and every exact HTTPS origin it may expose:

```toml
[browser]
resource = "personal/research"
allowed_origins = ["https://example.com"]
allow_public_https_research = false
allow_masked_visual_observations = false

[tools]
allow = [
  "browser_session_open_resource",
  "browser_session_close",
  "browser_navigate",
  "browser_snapshot",
]

[permissions]
allow_mutating = ["browser_session_open_resource"]
```

Opening authenticated persistent state is an explicit standing disclosure even though the rest of
the surface is read-oriented. A schedule approval records the exact resource configuration digest,
origins, provider disclosure, tools, and browser budget; widening or changing that scope requires
reapproval. A resource revision change also changes the runtime-policy digest.

To use `browser_visual_snapshot`, set `allow_masked_visual_observations = true`, include the tool,
and list the job's pinned provider in the owning profile's
`browser.screenshot_allowed_providers`. The selected model must independently accept images.

## Configure results and recurring inputs

When `messaging.job_route` is configured, named runs enqueue an automatic terminal result
through that route by default. To keep a job's terminal results available only through history and reports,
add this top-level setting:

```toml
result_notification = "never"
```

The supported values are `"always"` and `"never"`; omitting the setting is equivalent to
`"always"`. This controls only Ricky's automatic completed, failed, or approval-required job
result. It does not suppress an explicit `notify_user` call or a notification step in the job's
workflow. Every run still records its outcome, final message or error, transcript, workflow links,
and effect evidence under the existing retention rules, so `job history` and `job report` remain
available for offline inspection.

Job agents write terminal reports as mobile-first portable Markdown. They put status, key findings,
and required user action first. They may use headings, emphasis, lists, task lists, compact tables,
quotes, links, code, formulas, and footnotes when useful. Tables should have no more than three
short columns; large reports and media should use attachments. Telegram renders this portable
format, and future messaging transports can render or safely downgrade the same content.

Agent jobs can select recurring inputs with `[[task_sources]]` and
`[[stream_sources]]`. Task sources find eligible tasks; they do not assign or claim
them. Slack streams retain a cursor and bounded batches so a later run can account
for previously seen items. For example, add either source to an agent job:

```toml
[[task_sources]]
name = "weekly-review"
tags_any = ["queue:weekly-review"]
execution_modes = ["agent", "joint"]
limit = 10
reconsider_after_hours = 24

[[stream_sources]]
name = "team-channel"
adapter = "slack_channel"
channel_id = "C0123456789"
initial_lookback_hours = 24
item_limit = 50
```

Configure Slack in the job's scope before adding a Slack source. Give the job the
read tools it needs and exact mutation permissions if it must advance tasks.
Ricky injects source-accounting tools; the job records a disposition for each
candidate or stream item instead of writing cursors directly. Workflow-backed jobs
cannot declare these sources; pass their inputs through workflow arguments.

Task-source `due_before` values must include a timezone offset, such as
`2026-08-25T17:00:00+00:00`. Ricky normalizes offset-aware values to UTC before comparing source
approval scopes.

Each run records its complete profile scope. A schedule pins that scope together with the job and
runtime-policy digests as its exact validated execution revision. A workflow job also records its
resolved arguments, workflow run ID, terminal workflow status, context lineage, and context
revision. Workflow checkpoints and effect journals remain owned by the workflow run rather than
being copied into the job ledger.

## Run a workflow through a job

Keep stable arguments in the job definition. Ricky uses them exactly and never asks the resolver
model to replace them. The resolver supplies only omitted fields from the job goal, workflow
argument descriptions, accessible profile resources, and bounded metadata from the previous
successful run. Workflow defaults apply when an omitted optional field is not returned.

```toml
version = 3
name = "personal-email-triage"
description = "Triage new personal email through the reviewed workflow."
goal = """
Triage unread messages received since the last successful execution.
Use the supplied workflow and return its concise terminal report.
"""

[context]
lineage = 1
revision = 1

[budget]
wall_clock_seconds = 900
iterations = 3
max_completion_tokens_per_request = 4096
effect_calls = 50

[workflow]
name = "email-triage"

[workflow.args]
account = "personal/personal"

[permissions]
allow_mutating = [
  "gmail_modify_labels",
  "gmail_trash",
  "remember",
  "park_for_review",
]
```

Do not add `[tools].allow` to a workflow job. Ricky derives tools from the compiled graph and
requires every non-read-only tool to appear in `allow_mutating`. A tool marked
`unattended = "forbidden"` makes the job invalid regardless of risk class.

An unattended workflow cannot contain an `approval` step. Producing a draft or parking a durable
task for later review is allowed because the workflow can finish without a synchronous response.
A workflow that performs an authorized mutation directly is also allowed. Refactor or create an
unattended workflow variant when an existing workflow contains an interactive approval step.

The job's wall-clock and external-effect limits are outer bounds. The workflow continues to own
its step-level model attempts, agent iterations, retries, checkpoints, and recovery state.

Keep recurring jobs narrow. Prefer exact selectors and small budgets, and validate after any
change. Configure `messaging.job_route` to send durable job-result notifications through a static
messaging route.

## Schedule a named job

Schedules require a POSIX host with `crontab`, Python's `fcntl` support, and the installed
`ricky` executable on `PATH` when synchronizing. Ricky records its absolute path in cron.
Cron uses the host timezone; the `user_timezone` setting does not change cron timing.

Create desired state, install it, and verify it:

```bash
ricky job validate project-brief
ricky schedule add project-brief --cron "0 8 * * 1-5"
ricky schedule list
ricky schedule sync
ricky schedule doctor
```

`schedule add` validates the current execution revision and approves its authority envelope and
cron expression. It does not edit your crontab. `schedule sync` performs the reconciliation.

The approval envelope records the resolved provider, exact user-facing tool contracts, standing
mutations, private-data source scope, locked workflow arguments, external-effect ceiling, and—for
jobs with Gmail or Calendar tools—the issued profile-qualified Google account ids and expected
email identities. The model is part of the execution revision but not the approval envelope when
it stays inside the same provider trust boundary.

Adding an account visible to a Gmail or Calendar job, or changing the configured email identity
behind an existing account id, requires schedule approval. Removing an account requires validation
only. Jobs without account-capable tools do not drift when the profile's Google account inventory
changes. This is an approval snapshot, not a `JobSpec` account allowlist: the pinned profile scope
remains the hard access boundary, and the model may choose among the approved accounts from the
goal. A future job resource filter can narrow that set without changing this boundary.

Ricky's injected `record_item_disposition` and `record_candidate_disposition` tools are different:
they are batch-confined internal accounting controls, not user-granted capabilities. Their exact
contracts participate in the execution revision, so changes produce `validation_required` rather
than `approval_required`. Validation fails if either tool stops being a confined Ricky-state-only
mutation.

## Change or remove a schedule

```bash
ricky schedule show SCHEDULE_ID
ricky schedule set SCHEDULE_ID --cron "30 8 * * 1-5"
ricky schedule refresh SCHEDULE_ID
ricky schedule approve SCHEDULE_ID
ricky schedule disable SCHEDULE_ID
ricky schedule enable SCHEDULE_ID
ricky schedule remove SCHEDULE_ID
ricky schedule sync
```

`set`, `enable`, `disable`, and `remove` change desired state only. A timing change requires
explicit schedule approval. Run `schedule sync` afterward.

A changed job can report one of these gates:

| State | Required action |
|---|---|
| `lineage_required` | Choose preserve or start-fresh in the job's `[context]`, then validate again. |
| `validation_required` | Run `schedule refresh` to validate and pin a revision inside the existing approval envelope. |
| `approval_required` | Review and explicitly approve expanded authority, a new trust boundary, changed locked arguments, or schedule timing. |

For a validation-only update, run:

```bash
ricky job validate JOB_NAME
ricky schedule refresh SCHEDULE_ID
ricky schedule sync
```

Refresh fails closed if the revision exceeds the prior approval envelope. For an authority or
timing change, run:

```bash
ricky schedule approve SCHEDULE_ID
ricky schedule sync
```

Adding or changing a user-facing tool contract, adding standing mutation authority, widening
private-data selectors, issuing another Google account to an account-capable job, increasing the
effect ceiling, changing provider, or changing locked workflow arguments requires approval.
Removing tools or accounts, narrowing selectors, lowering bounds, changing an internal
bookkeeping-tool contract, changing a model within the same provider, editing a goal, or changing
workflow implementation without authority expansion requires validation and refresh only.
`schedule sync` never validates or approves a changed revision.

## Understand the cron boundary

Ricky accepts a numeric five-field cron subset with lists, ranges, and steps. It rejects aliases
such as `@daily`, month or weekday names, comments, assignments, `%`, and embedded commands.

Ricky owns only its marked crontab block, preserves unrelated entries, creates a backup before a
change, and verifies the installed result. Remove the managed block while retaining desired
schedules with:

```bash
ricky schedule uninstall
```

Cron controls due-time behavior. Ricky does not provide missed-run catch-up, jitter, schedule-level
retry, or high availability. Launcher logs live under
`<user_data_dir>/cron/logs/<schedule-id>.log`.
