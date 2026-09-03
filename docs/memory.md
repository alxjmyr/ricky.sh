# Use memory

Memory stores durable facts that can help in later chats. Use it for stable preferences,
relationships, account facts, project context, decisions, and commitments. Use durable tasks for
work that needs an owner or completion state.

## Save a fact

In chat, run:

```text
/remember I prefer concise answers with commands before explanation.
```

Ricky turns the request into a structured note and shows a permission prompt before writing it.
Review the destination profile, title, summary, and body. You can also ask naturally:

```text
Remember in my work profile that Northwind's renewal owner is Dana Chen.
```

Run `/remember` without text to review the conversation for useful facts. Ricky proposes notes but
does not save them during that turn. Reply with the items you approve or the edits you want.

## Recall information

Ask Ricky to recall a subject in natural language:

```text
What do you remember about Northwind?
```

Ricky receives a bounded index from only the profiles in the session scope. It loads full note
bodies with the read-only `recall` tool when they are relevant. Recall uses deterministic keyword,
slug, type, profile, and tag matching. It does not use an embedding service or send the complete
memory store to a third party for retrieval.

## Choose profile access

Every session includes `shared` and one primary profile. Start a work session with:

```bash
ricky chat --profile work
```

Add another readable profile when one session needs an aggregate view:

```bash
ricky chat --profile work --access-profile personal
```

The primary profile supplies the persona and ordinary write destination. Additional profiles add
readable resources but do not inject their persona. Put a fact in `shared` only when you intend it
to be available in every Ricky context. The selected model must be allowed for the complete scope;
provider choice never grants profile access.

Ricky normally selects the write profile from the request, active resource, and primary profile.
It asks a short clarification only when multiple plausible destinations would materially change
the result.

## Inspect memory configuration

```bash
ricky config memory
```

This command shows each enabled profile's memory root and read-only note count. It does not call a
model or rebuild indexes.

## Update or delete memory

Ask Ricky to remember a complete replacement for an existing note. Ricky preserves the creation
time and updates the modification time.

Ask Ricky to forget a note only when you want to delete it:

```text
Forget the outdated work-profile note about Northwind's renewal owner.
```

`forget` is destructive and requires permission. Memory has no built-in trash or undo. Back up
`<user_data_dir>/profiles/` before bulk changes.

## Understand storage

Memory notes are Markdown files with TOML frontmatter under:

```text
<user_data_dir>/profiles/shared/memory/
<user_data_dir>/profiles/personal/memory/
<user_data_dir>/profiles/work/memory/
```

Ricky derives indexes from the notes. Do not store runtime memory under the project `.ricky/`
directory. Set `[memory] enabled = false` in a profile's `ricky.toml` to disable memory whenever
that profile is in scope.
