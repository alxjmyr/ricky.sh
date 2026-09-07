# Use skills

A skill is a reusable instruction bundle. Use a skill when Ricky should apply a consistent method,
voice, or checklist but still needs model judgment about the sequence of work.

Use a [workflow](workflows.md) instead when order, typed handoffs, retries, or approval gates must be
enforced by code.

## List skills

Start a chat, then run:

```text
/skill
```

Ricky lists every loaded skill and reports malformed bundles separately.

## Activate a skill

```text
/skill repo-orient
```

You can pass free-form arguments after the name:

```text
/skill humanizer Use a direct, friendly tone for a technical audience.
```

One skill is active at a time. Activating another skill replaces the current skill. The active
skill shapes later model requests until you replace it, run `/clear`, or exit the chat.

Ricky can also activate a skill itself when the skill catalog clearly matches your request. The
same instructions and resource boundaries apply.

## Use the bundled skills

Ricky ships with skills for:

- Authoring and updating workflows
- Authoring and updating jobs
- Looking up Ricky usage, configuration, and troubleshooting with `ricky-docs`

Bundled skills are part of the distribution and are available in every
installation, whatever directory you start Ricky from. They carry the reserved
`bundled` owner and the qualified name `bundled/<name>`.

Run `/skill` for the exact names and descriptions available to you.

### Ask Ricky about itself

In chat, activate the docs skill, then ask your question:

```text
/skill ricky-docs
How do I configure a Google account in my personal profile?
```

You can also ask naturally, such as “Use the Ricky docs to diagnose why my schedule
isn't running.” Ricky can select the skill automatically, including in gateway
conversations when its capability policy allows it. `ricky ask` has no skills or tools.

The skill ships with the user guides and configuration examples from the installed
release. It works offline and uses `search_skill_resources` to find literal keywords,
then `read_skill_resource` to read the relevant section. Search returns file names and
line numbers. It skips symlinks and reports when scan limits or unreadable files make
results incomplete. The generated section index provides another way to browse.

An upgrade replaces the bundled docs with the new release's copy. They are read-only;
edit live configuration under `user_data_dir`, not the skill's references. A user skill
named `ricky-docs` can override the bundled one; use `/skill bundled/ricky-docs` to
select the released instructions explicitly. The skill does not grant additional
permissions or repair startup failures that prevent a conversation from running.

## Discovery order

Ricky discovers skills in this order:

1. `<user_data_dir>/profiles/<profile>/skills/<name>/SKILL.md` for every accessible profile
2. The skills bundled with Ricky

A skill you author with the same bare name takes precedence over a bundled
skill. The bundled skill stays reachable as `bundled/<name>`.

## Create a skill

Create this structure below the profile that should own the skill:

```text
<user_data_dir>/profiles/<profile>/skills/my-skill/
  SKILL.md
  scripts/          # optional; subject to Ricky's normal tool and permission boundaries
  references/       # optional
  assets/           # optional
```

Use this `SKILL.md` format:

```markdown
---
name: my-skill
description: Review documents for clarity and factual support. Use when revising prose.
---

Review the requested document.

1. Identify the intended audience and outcome.
2. Flag unsupported claims.
3. Propose the smallest clear revision.
```

The bundle directory must match `name`. The `name` must contain no more than 64 lowercase letters,
numbers, and hyphens. The `description` must contain no more than 1,024 characters and should
explain both what the skill does and when Ricky should use it.

Ricky uses the Agent Skills `SKILL.md` format. `license`, `compatibility`, `metadata`, and
`allowed-tools` are optional frontmatter fields. `allowed-tools` is portable metadata only: it does
not bypass Ricky's capability registry or permission checks.

Keep instructions direct and scoped. Tell Ricky what outcome to produce, what evidence to inspect,
which constraints matter, and when to stop.

## Add supporting resources

A skill can refer to passive files inside its bundle. Ricky reads them on demand with a
bundle-confined resource tool. Resource paths cannot be absolute and cannot contain `..`.
It can also search text files inside the active bundle with `search_skill_resources`.

Do not put credentials or generated state in a skill bundle. Portable bundles may include scripts,
but a skill cannot register a script as a Ricky tool or execute it outside Ricky's normal tool and
permission boundaries.

## Create a profile skill

Put the bundle under:

```text
<user_data_dir>/profiles/personal/skills/my-skill/SKILL.md
```

The containing profile owns the skill. Put universally applicable skills under `profiles/shared/`.
A skill for project-specific work still belongs in a profile directory. Ricky does not
discover skills from the project. Use a qualified name when multiple accessible profiles
contain the same local skill name.

Restart chat after you create or edit a skill. Ricky discovers skills when it constructs the chat
runtime.
