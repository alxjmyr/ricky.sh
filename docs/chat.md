# Chat with Ricky

Use chat for interactive work that needs files, shell commands, integrations, memory, skills, or
workflows.

## Start a chat

```bash
ricky chat
```

Override the configured model for this chat when needed:

```bash
ricky chat --provider anthropic --model claude-sonnet-5
```

Choose a primary profile and optional additional readable profiles when needed:

```bash
ricky chat --profile work --access-profile personal
```

The profile scope, provider, and model are pinned when the chat starts. You cannot switch them
without starting a new chat. See [Profiles](profiles.md).

## Give Ricky an effective request

State the outcome, the relevant location or system, and important constraints. For example:

```text
Review the Python files under src/ricky/memory. Explain how profile access is enforced. Do not
change any files.
```

For changes, include a success condition:

```text
Add a concise usage example to README.md. Preserve the existing structure and verify that every
documented command exists.
```

Ricky can plan complex work with its session task list. This task list is temporary working state;
it is not the same as a [durable task](durable-tasks.md).

## Edit a prompt

Chat provides multiline editing in an interactive terminal. Paste multiline text directly; Ricky
keeps the complete paste in the composer and sends it only when you press `Enter`.

| Key | Action |
|---|---|
| `Enter` | Send the complete prompt. |
| `Ctrl+J` | Insert a newline. |
| `Alt+Enter` or `Esc`, then `Enter` | Insert a newline when the terminal reports the Alt modifier. Use `Ctrl+J` if it does not. |
| Arrow keys | Move through characters and lines. Up and down recall input history at prompt boundaries. |
| `Ctrl+Left` or `Ctrl+Right` | Move backward or forward by one word. |
| `Ctrl+A` or `Ctrl+E` | Move to the beginning or end of the current line. |
| `Ctrl+K` or `Ctrl+U` | Delete from the cursor to the end or beginning of the current line. |
| `Ctrl+W` or `Alt+Backspace` | Delete the previous word. |
| `Alt+D` | Delete the next word. |
| `Ctrl+Z` or `Ctrl+Y` | Undo or redo an edit. |
| `Ctrl+R` | Search input history from this chat. |
| `Ctrl+X Ctrl+E` | Edit the draft with `$VISUAL` or `$EDITOR`, then return to the composer. |
| `Tab` | Complete a slash command or a skill name after `/skill`. |
| `Ctrl+C` | Clear a non-empty draft. At an empty prompt, interrupt as described below. |

Input history exists only for the current process and is discarded when chat exits. Ricky does not
write prompt history to disk. Answers to permission, approval, and selection prompts are kept out
of chat history. Enhanced editing is disabled when input or output is redirected so scripts and
captured output retain ordinary stream behavior.

## Read agent output

While the model is producing a response, chat shows a transient `Thinking…` indicator. Tool calls
and their results remain visible as they happen, with a transient running indicator during longer
tools. Ricky renders each completed model response as Markdown. If one turn contains an explanation,
tool calls, and a later answer, each model response appears in sequence around the tool events.

If a provider fails or a turn is interrupted after returning partial text, chat preserves that text
and labels it as a partial response. Redirected output omits transient indicators.

## Work with project and host files

The terminal chat has no separate file-attachment command. Ask Ricky to read a file by its
workspace-relative path, a `~` home-relative path, or an absolute path:

```text
Read reports/quarterly.md and summarize the risks.
```

```text
Read ~/Documents/quarterly.md and summarize the risks.
```

Reads, directory listings, path searches, and text searches inside the active project run
automatically by default. The same tools can work elsewhere on the host, including below
`user_data_dir`, but Ricky asks for permission using the exact canonical path first. Symlinks are
authorized as their resolved destination. Writes and exact edits require permission wherever they
target. Parent directories must exist before Ricky writes a new file.

When a text tool result is too large for model context, Ricky saves the complete result as a
session artifact and sends the model a bounded excerpt plus an opaque artifact ID. Ricky can page
through the stored result with its artifact reader. Ask it to continue reading if the excerpt omits
needed content.

The terminal composer remains text-only; it has no general image-upload command. A permitted
browser visual snapshot can add an opaque image reference as Ricky-authored follow-up content in
the same chat. At most the latest two images that fit the configured media and context ceilings are
projected into a provider request. `/clear` and runtime shutdown remove browser screenshot media;
durable browser downloads are separate and remain under their owning profile. The browser resource
owner must allow the chat's pinned provider, and the pinned model must accept image input. See
[Browser control](browser-control.md#use-visual-fallback) for the disclosure and model-selection
steps.

## Review tool use

Read-only tools run automatically by default. When Ricky proposes a mutating or destructive tool,
the prompt shows:

- The tool name
- A summary of the proposed action
- Ricky's reason for using the tool
- Any session-wide grant choices the tool safely supports

Use `y` to allow the call once or `n` to deny it. Some prompts also offer:

- `a` for a scoped session grant, such as writes to one exact path
- `d` for reads or searches below one resolved directory
- `A` for the whole tool during this session, when the tool permits that breadth

Grant choices are case-sensitive. Use the narrowest choice that fits the task.

## Use chat commands

Enter these commands at the `ricky>` prompt:

| Command | Action |
|---|---|
| `/help` | List chat commands. |
| `/debug` | Toggle detailed event rendering. You can also use `/debug on` or `/debug off`. |
| `/tasks` | Show the current chat's temporary task list. |
| `/context` | Inspect the assembled context without calling the model. |
| `/compact [focus]` | Summarize older context while keeping a recent verbatim tail. |
| `/model` | Show the provider and model pinned to this chat. |
| `/clear` | Discard the current chat state and start a fresh session with the same model. |
| `/permissions` | List active session permission grants. |
| `/permissions clear` | Revoke all active session permission grants. |
| `/remember <text>` | Ask Ricky to save one durable fact. |
| `/remember` | Ask Ricky to propose memory notes for your review. |
| `/skill` | List loaded skills. |
| `/skill <name> [args]` | Activate a skill for later turns. |
| `/workflow` | List loaded workflows. |
| `/workflow <name> [key=value ...]` | Run a workflow. |
| `/quit` | Exit the chat. `/exit` and `/q` also work. |

## Inspect model context

Use `/context` when you need to understand what Ricky will send to the model. The report includes
the context sections, estimated token use, capacity source, configured reserve, and retained versus
projected image counts, bytes, pixels, and token estimates.

Use `/debug` when diagnosing a specific turn. Debug mode displays context assembly, request
metadata, token usage, normalized tool arguments, and tool results. Debug output can contain
non-secret user data returned by tools; handle terminal logs accordingly.

## Compact a long chat

Use `/compact` when older conversation history consumes too much context:

```text
/compact Preserve every decision about the release process.
```

Compaction asks the current model to summarize an old prefix of the chat. Ricky keeps a recent
complete-turn tail verbatim and records a checkpoint. Compaction is explicit; Ricky does not
silently replace history while you chat.

## Stop work safely

Press Ctrl+C during a turn to cancel that turn. At an idle prompt, Ctrl+C first clears a non-empty
draft. With an empty draft, press Ctrl+C twice to exit. Ricky cancels owned work and releases
durable-task leases when the chat closes cleanly.

## Understand chat persistence

`ricky chat` is ephemeral. Exiting discards its transcript, temporary tasks, active skill, context
checkpoints, and session permission grants. The following data can survive because it is stored
separately:

- Memory notes
- Durable tasks and their artifacts
- Workflow run checkpoints
- Integration downloads

The `ricky session` commands apply to persistent conversations created by the gateway, not ordinary
terminal chats. See [Messaging and the gateway](messaging-and-gateway.md).
