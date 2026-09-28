# Run workflows

A workflow is a versioned, typed execution graph. Use one when the process needs an enforced order,
structured handoffs, branching, parallel work, approvals, or safe recovery.

Use ordinary [chat](chat.md) or a [skill](skills.md) for open-ended work whose steps depend on
ongoing judgment.

## Discover workflows

```bash
ricky workflow list
```

Use `/workflow` in CLI or Telegram chat to list workflows. Listings show
`[built-in]` or the owning profile beside each name, and use qualified names
when names repeat. Telegram listings are read-only; run workflows there through
a named workflow-backed job.

Ricky discovers bundles in this order:

1. `<user_data_dir>/profiles/<name>/workflows/<workflow-name>/workflow.toml` for every accessible profile
2. The workflows bundled with Ricky

Workflows use qualified names such as `work/email-triage`. An unqualified name is accepted only
when it resolves to one accessible workflow. A bundled workflow carries the reserved `bundled`
owner and the qualified name `bundled/<name>`. A workflow you author with the same bare name
takes precedence over a bundled workflow, which stays reachable as `bundled/<name>`.

Discovery checks required tools against the current profile scope. If a tool is unavailable,
Ricky reports the missing tools and setup guidance under **Workflow discovery issues**. Check
integration configuration and profile access; for custom workflows, also check tool names.
The bundled email-triage workflow requires a [configured Google account and matching OAuth
credentials](integrations.md#connect-google-accounts) in an accessible profile. After setup,
retry `ricky workflow list` with `--profile` or `--access-profile` as needed. The source path
identifies the affected workflow; an unavailable tool does not mean that its file is missing.

The bundled email-triage workflow reads whole threads for context, but marks read or
trashes only each selected search-result message. Other messages in the thread are
not changed. Message IDs also distinguish job effects when a new message arrives
in a previously processed thread. Review tasks remain grouped by thread.

When upgrading from thread-based email triage, existing receipts remain valid and
need no migration. For scheduled jobs that use this workflow, increment the job's
`context.revision`, validate it, then run `schedule refresh` and `schedule sync`
with the same profile and project options. Until the changed definition is
acknowledged, the schedule can report `lineage_required`. Saved workflow runs with
the old graph cannot resume against the new graph; start a new run instead.

## Inspect and validate a workflow

Always validate before you run a new or changed workflow:

```bash
ricky workflow validate email-triage
ricky workflow show email-triage
```

`validate` checks the graph, typed references, schemas, tool availability, conditions, retries, and
effect contracts. `show` renders roots, dependencies, step kinds, conditions, arguments, risks,
and outputs without running the workflow.

## Run a workflow

Pass each trigger argument with repeatable `--args` or `-a` options. Values are parsed as JSON when
possible:

```bash
ricky workflow run email-triage \
  -a account=personal/personal \
  -a limit=10
```

You can also run a workflow inside chat:

```text
/workflow email-triage account=personal/personal limit=10
```

Only one workflow can be active or queued in one chat at a time.

Gateway conversations cannot start workflows directly or through ad hoc delegation. They can
start a named workflow-backed job. That job resolves omitted arguments and invokes the workflow in
the background with its authored standing authority. The same job can be launched with `job run`
or a schedule. See [Run jobs and schedules](jobs-and-schedules.md#run-a-workflow-through-a-job).

Workflow tool steps use the same permission engine as chat. Approval steps add explicit human
review; they do not pre-authorize later tool calls.

An unattended workflow-backed job rejects every `approval` step. Draft generation and
`park_for_review` remain suitable because they finish without waiting for an inline response.
Mutating and destructive tool steps are eligible when the tool explicitly allows unattended use
and the job grants that exact tool.

Changing a workflow referenced by a named job changes that job's execution revision and context
definition. Preserve the job's prior conclusions by incrementing its `context.revision`, or start
fresh by incrementing `context.lineage` and resetting the revision to `1`. Then validate the job.
Use `schedule refresh` when the compiled authority stays inside the existing approval envelope;
use `schedule approve` when tools, private-data scope, standing mutations, locked arguments,
provider boundary, or effect ceiling expand. Run `schedule sync` afterward.

## Dry-run a workflow

```bash
ricky workflow dryrun email-triage \
  -a account=personal/personal \
  -a limit=5
```

A dry run validates mutating or effectful tool arguments and records `would_dispatch` without
requesting permission or invoking the tool. It denies approval steps. It can still call the
configured model and real read-only tools, including network tools. A dry run can therefore
consume tokens and read current external data.

The email-triage Google account argument is required and profile-qualified. In chat, Ricky receives
a catalog of the accounts available to that session and should map phrases such as “my personal
inbox” to the exact id, such as `personal/personal`. CLI and slash-command invocations must supply
that exact id.

Use `--fixtures PATH` to supply canned step completions where the workflow supports them.

## Inspect and recover a run

Ricky persists workflow checkpoints below `user_data_dir` with their profile scope. Pass the same
`--profile` and `--access-profile` options used to start the run.

```bash
ricky workflow status RUN_ID
ricky workflow resume RUN_ID
ricky workflow abandon RUN_ID
```

Resume requires the same compiled workflow graph. Ricky refuses to resume after graph drift.

If an external effect started but its result is unknown, Ricky records the effect as in doubt and
does not replay it. Inspect the external system, then record what actually happened:

```bash
ricky workflow reconcile RUN_ID EXECUTION_ADDRESS --completed
ricky workflow reconcile RUN_ID EXECUTION_ADDRESS --not-completed
ricky workflow resume RUN_ID
```

Reconciliation is a statement of observed fact, not a request to perform the action.

A `performed` receipt is a permanent no-replay boundary, even if the provider returned an error or
an invalid result shape afterward. A known `not_performed` result can be retried only when the
compiled tool contract explicitly declares safe replay and the step has a remaining attempt.
Without that contract it fails rather than risking an unintended repeat.

## Understand workflow steps

Version 2 workflows compose these step kinds:

| Step | Purpose |
|---|---|
| `model` | Ask the model for one typed result without tools. |
| `agent` | Run an isolated bounded model task with an explicit read-only tool set. |
| `tool` | Call one registered tool with typed arguments. |
| `data` | Apply a deterministic built-in data operator. |
| `check` | Evaluate a typed condition or run a bounded shell check. |
| `approval` | Ask a person to confirm a proposal or select items. |
| `foreach` | Run a bounded subgraph for each item in a collection. |
| `message` | Render deterministic output. |

Steps declare dependencies through `needs`, conditions through `when`, and data references to the
trigger or prior step outputs. Result schemas prevent prose-shaped output from silently entering a
typed downstream step.

## Author a workflow

Start with the included authoring skill:

```text
/skill author-workflow
```

A bundle has this shape:

```text
<user_data_dir>/profiles/<profile>/workflows/my-workflow/
  workflow.toml
  instructions/     # optional longer step instructions
  references/       # optional passive resources
```

The file must declare `version = 2`, and its `name` must match the bundle directory. Prefer the
smallest graph that makes the important order and safety boundaries explicit. Declare schemas for
model output, keep tool arguments typed, isolate external effects in tool steps, and add approval
before irreversible actions.

The bundle location establishes profile ownership; `workflow.toml` has no profile field. Use
`shared` only when the workflow should be available in every runtime, and use qualified names when
more than one accessible profile contains the same local name.

After every edit, run `workflow validate`, inspect `workflow show`, then use `workflow dryrun`
before a live run.

When an edited workflow is referenced by a named job, also follow the `update-job` skill. A valid
workflow alone does not resolve the job's lineage decision or refresh its scheduled execution
revision.

## Visualize a workflow design

Use the bundled `visualize-workflow` skill to explore a workflow while designing
or revising it. Ask Ricky to show the workflow, then ask for a step's instructions,
inputs, output schema, consumers, conditions, or loop body. The simple view uses
ASCII and fits CLI and gateway chats. Focused details include the full resolved
instruction file and attached skill guidance.

```bash
ricky workflow visualize shared/email-triage
ricky workflow visualize shared/email-triage --step classify --section instructions
ricky workflow visualize shared/email-triage --step classify --section outputs
ricky workflow visualize shared/email-triage --html
ricky workflow visualize shared/email-triage --open-chrome
```

Replace the example identity and step with your own. Add `--profile` and repeated
`--access-profile` flags for the intended scope. Supported text sections are `all`,
`instructions`, `inputs`, `outputs`, `policy`, and `body`. Omit `--step` for the
compact dependency map. Existing `workflow show` output is unchanged.

The HTML blueprint is a self-contained file with light and dark themes, search,
zoom and pan, expandable loop bodies, and a step inspector. Select a connection
to see the exact field mapping. Dependency lines show ordering; data lines show
bindings; dotted condition lines show conditional reads. Selecting a step highlights
its data sources and consumers. Full instructions, skill guidance, input expressions,
and schemas remain available in the inspector.

Visualization reads current sources and compiles the design without running any
steps, shell checks, models, or workflow tools. Invalid definitions report compile
errors. References remain unresolved: the document describes the declared context,
not a populated prompt or a specific execution. A revision fingerprint includes
resolved instructions and skill bodies. After editing a workflow, generate a new
view; an already-open HTML file is a snapshot.

`--open-chrome` exports the view and hands it to ordinary desktop Chrome on the
host running Ricky. The command returns without waiting for Chrome to close, so
you can continue the chat while the blueprint remains open. It does not use a
managed automation browser. To open a file
already returned by `render_workflow`, use:

```bash
ricky workflow open-view '<exported-html-path>'
```

Use the same profile scope that created the export. Host Chrome needs a graphical
desktop; opening it from a gateway does not open a browser on your phone. Ask Ricky
to send the HTML as an attachment through an available authorized messaging tool
instead. Save the file and open it in a browser; no server or network connection
is required.

Exports live below `user_data_dir` in `workflow.visualization_dir` (default:
`workflow-views`). They are partitioned by the complete profile scope, so an export
containing guidance from another profile is not available to a narrower scope.
Export files are content-addressed and checked before opening or attachment delivery.
You can delete old exported HTML files when no longer needed; regeneration does not
change the workflow or any run history.
