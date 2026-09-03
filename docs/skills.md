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

Bundled skills are part of the distribution and are available in every
installation, whatever directory you start Ricky from. They carry the reserved
`bundled` owner and the qualified name `bundled/<name>`.

Run `/skill` for the exact names and descriptions available to you.

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

Do not put credentials or generated state in a skill bundle. Portable bundles may include scripts,
but a skill cannot register a script as a Ricky tool or execute it outside Ricky's normal tool and
permission boundaries.

## Create a profile skill

Put the bundle under:

```text
<user_data_dir>/profiles/personal/skills/my-skill/SKILL.md
```

The containing profile owns the skill. Put universally applicable skills under `profiles/shared/`.
Project skills are owned by the primary profile; use a qualified name when multiple accessible
profiles contain the same local skill name.

Restart chat after you create or edit a skill. Ricky discovers skills when it constructs the chat
runtime.
