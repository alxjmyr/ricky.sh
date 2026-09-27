---
name: visualize-workflow
description: Explore a Ricky workflow design as a compact ASCII graph or interactive HTML blueprint, and inspect step instructions, skill guidance, data bindings, schemas, and loops while designing or revising a workflow.
---

# Visualize a workflow design

Use `inspect_workflow` for the simple view and conversational follow-ups. Use
`render_workflow` for the in-depth HTML view. These inspect current sources without
running steps. Prefer the qualified workflow identity returned by inspection in
subsequent calls. Refresh after edits; do not reuse details from an older snapshot.
If no workflow has been identified, use the workflow catalog or ask which one.

## Simple view and follow-ups

Start with `inspect_workflow(name=...)` and present the compact ASCII graph in a
plain monospaced block. Keep mobile replies short. Expand only requested detail:

- Exact instructions or classification guidance: `step=<id>, section="instructions"`.
  This includes resolved instruction files and attached skill guidance.
- Data received: `section="inputs"`; outputs and downstream consumers:
  `section="outputs"`.
- Conditions, failure handling and retries: `section="policy"`.
- A loop's item graph: `section="body"` on the `foreach` step.
- Complete detail: `section="all"` on the selected step.

Resolve conversational references such as "the classifier" to an actual step id.
If ambiguous, ask which step. Preserve exact guidance when the user requests it;
label summaries as summaries. Use further focused calls instead of repeating the
entire workflow. If a long result is offloaded, use the normal artifact reader to
retrieve the requested instructions in full.

Distinguish ordering (`needs`), bound data, and conditional reads. A dependency
alone passes no data. References describe future values, not observed results.
Model tasks inherit neither this chat nor memory. Explain design strengths or gaps
using retrieved declarations; separate your assessment from the authored facts.
Compilation errors are design feedback: report them without inventing a graph or
running the workflow to diagnose it.

## In-depth HTML blueprint

Call `render_workflow` to create the offline HTML file. It includes both themes,
search, expandable loops, full instructions, field mappings and schemas. Return the
actual exported path. Never write into a workflow or skill bundle to store a view.

When the user asks to open the view in Chrome on Ricky's host, use `run_shell` to
run `ricky workflow open-view '<exact-returned-path>' <scope-flags>` with normal
shell quoting. In a development checkout use `uv run ricky`. Scope flags are
`--profile <current-primary>` and one `--access-profile <name>` for every additional
profile in the current session. Never broaden scope. Opening transfers the document
to ordinary desktop Chrome; it does not use or take over a managed browser resource.
Report a failed open accurately and keep the exported path available.

In gateway/mobile chat, a host browser window does not open on the user's phone.
For a request to deliver the HTML, use an available authorized messaging tool with
the returned path as a file attachment. The file is self-contained; the user can
open it in a browser after saving it. Do not send it to a guessed destination or
claim a local file URI is remotely accessible. If delivery tools are unavailable,
explain that limitation and provide the simple view.

Direct CLI equivalents:

```text
ricky workflow visualize <qualified-name>
ricky workflow visualize <qualified-name> --step <step-id> --section instructions
ricky workflow visualize <qualified-name> --html
ricky workflow visualize <qualified-name> --open-chrome
```

Append the same scope flags to CLI calls. Existing `workflow show`, validation,
execution and status commands retain their meanings; this skill inspects designs,
not individual executions.
