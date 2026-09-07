---
name: author-workflow
description: Interview the user about a repeatable process, create or convert a project- or profile-owned typed Workflow V2 execution DAG, validate it within the intended profile scope until clean, and show the compiled graph for approval. Use when the user asks to create, redesign, or migrate a Ricky workflow.
---

# Author a Workflow V2 execution graph

Create only Workflow V2 bundles. Every bundle must declare `version = 2`.
Do not create or maintain V1 state-machine definitions. If an existing bundle
has no version, convert the complete bundle to V2 before you change its
behavior.

Use installed `ricky` commands. In a development checkout, follow its contributor
instructions and use `uv run ricky` (for example, `uv run ricky config`).
All user bundles belong below the resolved profile root; projects are not
discovery roots. Bundled resources are read-only. To customize one, copy its
complete bundle into an accessible profile and use that qualified identity.

## Procedure

1. Interview the user before you write files. Identify:
   - the invocation arguments and their types;
   - the required outputs and terminal result;
   - the steps and dependency edges;
   - the decisions that require model judgment;
   - the decisions that code can make;
   - the external reads and effects;
   - the approval surfaces;
   - the expected collection sizes and stable item keys;
   - the failure, retry, interruption, and resume behavior.
   - the owning profile and which project, if any, supplies the working directory;
   - every additional profile whose data or resources a run must access.
2. Propose the execution graph in plain language. Show its roots, fan-out,
   fan-in, conditions, approvals, effects, and data flow. Get the user's
   agreement before you write files.
3. Define each cross-step value before you author the graph. Give model and
   agent steps only the inputs they require. For each `foreach`, define a
   compact output projection that contains only values later steps require.
4. Choose a name that matches `^[a-z0-9][a-z0-9_-]{0,63}$`. Confirm that the
   current session's pinned profile scope contains the owner and every profile
   the authoring work must inspect. If it does not, stop and tell the user to
   start an appropriately scoped session; a skill cannot widen its runtime
   scope. Run `ricky config`, read `user_data_path`, and use
   `<resolved-user-data-dir>/profiles/<owner>/workflows/<name>`. Use `shared`
   only when the workflow is intentionally available in every runtime. Never
   guess or hardcode the user data root.
5. Call `run_shell` with `mkdir -p -- '<bundle-path>'`, after replacing the
   placeholders with the exact resolved path. Confirm that the command
   succeeds. Create a `steps` directory with the same command shape if the
   bundle uses instruction files.
6. Write `<bundle-path>/workflow.toml` with `write_file`. Write each optional
   instruction file separately so the user can review each write.
7. Define `<scope-flags>` as `--profile <primary>` plus one
   `--access-profile <name>` for each additional required profile. Call
   `validate_workflow` with the qualified `<owner>/<name>` after every draft;
   its session scope must match `<scope-flags>`. Also validate through
   `ricky workflow validate <owner>/<name> <scope-flags>`. Fix all
   reported errors and validate again. Stop only when validation returns the
   compiled graph and `valid`.
8. Show the validated graph to the user. Explain the declared model inputs,
   approval boundaries, effect steps, and `foreach` projections. Ask for
   approval before you declare the workflow ready.
9. After approval, use a fixture dry-run when fixtures are practical. Ask
   before a live dry-run that reads external user data. A dry-run can execute
   reads and model requests, but it must not dispatch effects.
10. If redesign or conversion changes a workflow already referenced by a named
    job, use `update-job` for that job before declaring unattended use ready.
    The user or an authorized job-owning agent must choose whether to preserve
    its context lineage or start fresh, then validate and refresh or reapprove
    each schedule according to the compiled authority impact.

## V2 authoring rules

### Bundle and graph

- Start with `version = 2`, `name`, and `description`. Do not use `entry`,
  transition tables, back-edges, or `__end__`.
- Do not add a `profile` key to `workflow.toml`. The bundle location
  establishes a profile workflow's owner; the invocation establishes a project
  workflow's owner and the complete runtime scope. Use `<profile>/<name>` in
  commands. Use an unqualified name only when it resolves to exactly one
  accessible workflow.
- Declare dependencies with `needs`. A step with no dependencies is a root.
  More than one root is valid.
- Keep the top-level graph and every `foreach` body acyclic.
- Use `dependency_policy = "success"` by default. Use `"terminal"` only when
  a reporting or recovery step must inspect failed or skipped branches.
- Use `when` for a pure conditional branch. Use only the supported closed
  operators: `equals`, `not_equals`, `in`, `exists`, `is_true`, and
  `is_false`.
- Keep every step id unique across the complete bundle, including all
  `foreach` bodies.

### Arguments, values, and results

- Declare every invocation argument in `[args.<name>]`. Set its `type` and
  model-facing `description`. Supported types are `string`, `integer`,
  `number`, `boolean`, and `string_list`.
- Give an optional argument a typed `default`. Apply `min`, `max`,
  `min_length`, `max_length`, or `values` when they provide a real bound.
- For a profile-owned resource such as a Google account, require the exact
  profile-qualified id exposed in the session's accessible resource catalog.
  Say so in the argument description and give a qualified example such as
  `personal/personal`. Do not use an unqualified local name as a default. A
  project or shared workflow that can run under multiple primary profiles
  should normally require the resource argument rather than silently choose
  one profile's account.
- Use typed value expressions. Read data with `{ ref = "..." }`. Use a
  `{ format = "...", values = {...} }` expression only to produce a string.
  Do not use `{{...}}` templates.
- Declare a named result schema for every `model` and `agent` step. Keep object
  schemas closed and bound variable-length strings and arrays where practical.
- Treat tool display text as display text. A downstream step can read only a
  tool's declared typed result data.
- Keep the complete checkpoint separate from step bindings. Persistent records
  support resume and audit. They are not implicit model context.

### Step selection

- Use `tool` for one registered tool call. Put every mutating or destructive
  operation in an explicit tool step so the normal permission and effect
  journal paths apply.
- Use `model` for one atomic judgment or generation request with no tools.
  Declare all inputs in `inputs` and validate the output with `result_schema`.
- Use `agent` only for a bounded investigation that needs allowlisted
  read-only tools. Never give an agent step an effect tool.
- Use `data` for deterministic `project`, `filter`, `partition`, `sort`,
  `limit`, or `merge` operations. Do not ask a model to perform these tasks.
- Use `check` for a deterministic condition or trusted authored shell check.
- Use `approval` with `confirm` or stable-key `select` mode for explicit user
  intent. Tool permission remains a separate authorization boundary.
- Use `message` for deterministic user-visible output.
- Use `foreach` for one nested DAG over a bounded collection. Do not nest
  `foreach` steps.

### Context and state discipline

- Give each model or agent step only the declared values it needs. Do not pass
  a complete prior step record when one field is sufficient.
- Always declare a non-empty `outputs` map on `foreach`. Project only values
  required by downstream steps. If no downstream data is required, project a
  small identity or completion value such as the item key.
- Do not project large read results, source objects, attempts, timestamps, or
  complete body step records unless a downstream contract requires them.
- Keep stable item identity outside the projection. Each projected item already
  contains `key`, `index`, `status`, and `error`; projected values appear under
  `output`.
- Remember that `workflow.max_binding_chars` applies to the current step's
  resolved bindings. Splitting unrelated work into explicit inputs prevents
  accidental context growth. Do not increase the limit to hide a broad data
  contract.

### Reliability and effects

- Set `on_error = "continue"` only when the graph has an explicit way to
  tolerate or report that failure.
- Set `on_item_error = "collect"` only when other items can complete safely
  and a later step reports collected errors.
- Add retries only for declared retry categories. Keep model repairs bounded.
  Do not retry an effect unless the registered tool declares safe replay and
  an idempotency key.
- Use proposal, approval, and effect as separate steps. An approval records
  user intent; the effect tool still passes through permission review.
- Use stable item keys for selection and resume. Never use a list index as the
  identity when the source has a stable id.

## Minimal V2 skeleton

```toml
version = 2
name = "example"
description = "Classify one target and report the selected path."

[args.target]
type = "string"
description = "Target to classify."
min_length = 1

[schemas.decision]
type = "object"
required = ["route", "reason"]

[schemas.decision.properties.route]
type = "string"
values = ["accept", "reject"]

[schemas.decision.properties.reason]
type = "string"
max_length = 500

[[steps]]
id = "classify"
kind = "model"
instruction = "Classify the target. Return only the declared JSON object."
inputs = { target = { ref = "trigger.target" } }
result_schema = "decision"
retry = { max_attempts = 2, on = ["invalid_output", "provider_error"] }

[[steps]]
id = "accepted"
kind = "message"
needs = ["classify"]
when = { ref = "steps.classify.output.route", equals = "accept" }
message = "Target accepted."

[[steps]]
id = "rejected"
kind = "message"
needs = ["classify"]
when = { ref = "steps.classify.output.route", equals = "reject" }
message = "Target rejected."

[[steps]]
id = "summary"
kind = "message"
needs = ["accepted", "rejected"]
dependency_policy = "terminal"
message = { format = "Decision: {route}", values = { route = { ref = "steps.classify.output.route" } } }
```

## Compact collection pattern

```toml
[[steps]]
id = "process"
kind = "foreach"
needs = ["source"]
collection = { ref = "steps.source.output.items" }
item_key = { ref = "item.source.id" }
outputs = { decision = { ref = "item.steps.classify.output" } }

  [[steps.body]]
  id = "classify"
  kind = "model"
  instruction = "Classify this item. Return only the declared JSON object."
  inputs = { item = { ref = "item.source" } }
  result_schema = "decision"
```

Pass `{ ref = "steps.process.output" }` to a data, approval, or later
`foreach` step. Inside a later item graph, read the projected value through
`item.source.output.decision`. Do not use an index or wildcard in a reference.
Downstream steps do not receive the complete item checkpoint.
