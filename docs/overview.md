# What is Ricky?

Ricky is a personal AI agent and agent harness. You give Ricky a goal in natural language. Ricky
uses a configured language model to reason about the goal and can use tools to inspect or change
the systems you make available.

Ricky is designed for work that benefits from both model judgment and code-enforced boundaries.
The model decides what to do. Ricky controls how tools run, which permissions apply, what context
the model receives, and which state is saved.

## Choose the right interaction

| You want to | Use | Why |
|---|---|---|
| Get one model response without tools | `ricky ask` | It sends one prompt directly to the selected model provider. |
| Work interactively with tools | `ricky chat` | It starts a full agent session with tools, permissions, memory, and skills. |
| Reuse a set of instructions | A skill | A skill guides the model while leaving the sequence flexible. |
| Run a repeatable, ordered process | A workflow | Ricky owns the step order, typed data flow, retries, and review gates. |
| Track work across sessions | A durable task | The task retains its objective, status, history, and artifacts. |
| Run bounded unattended work | A job | A job pins its goal, model, tool access, and resource budget. |
| Run a job at specific times | A schedule | Ricky verifies the job definition and installs a managed cron entry. |
| Talk to Ricky through messaging | The gateway | The gateway provides persistent conversations, a durable inbox, and durable delivery. |

## What Ricky can use

A full chat session includes these built-in capabilities:

- Read, search, create, and edit files using project-relative, home-relative, or absolute host
  paths.
- Run shell commands within the project.
- Maintain a temporary task list for the current chat.
- Recall and update durable memory.
- Discover and activate skills.
- Start and validate workflows.
- Create and coordinate durable tasks.

Ricky adds integration tools only when you configure their credentials:

- Slack
- Gmail
- Google Calendar
- Brave Search
- Telegram messaging through the gateway

## How Ricky handles risk

Tools declare a risk level. Read-only tools can run automatically. Tools that change files or
external services require permission unless a narrower policy already authorizes the action.
Destructive tools require explicit review.

At a permission prompt, inspect the tool name, exact action summary, and reason. Allow the action
once, grant an offered session scope, or deny it. Session grants end with that chat and can be
cleared with `/permissions clear`.

Reads inside the active project run automatically by default. Reading, listing, or searching
outside it requires approval for the resolved host path; Ricky can offer an exact-path or
directory-scoped session grant. Writes and edits continue to require permission wherever they
target.

Workflows do not bypass permissions. A workflow can restrict which tools are visible at a step and
can add an approval gate, but every tool still uses the normal permission path.

## How Ricky handles state

Ricky separates live user configuration, distributed capabilities, and generated state:

- `<user_data_dir>/ricky.toml` contains live non-secret configuration and policy.
- `<user_data_dir>/profiles/<name>/.secrets.toml` contains profile-owned credentials and must not be committed.
- `ricky.toml.example` and `.secrets.toml.example` are committed setup templates only.
- The skills, workflows, and jobs distributed with Ricky ship inside the package and are
  available in every installation. You author your own below
  `<user_data_dir>/profiles/<name>/`.
- `user_data_dir` also contains generated state such as memory, tasks, workflow runs, sessions,
  downloads, tokens, and databases. The default is `~/.ricky`.

An ordinary `ricky chat` session is ephemeral. Memory and durable tasks survive, but the chat
transcript does not become a resumable session. Persistent sessions are created and managed by the
messaging gateway.

## Next steps

Follow [Getting started](getting-started.md) to run your first prompt and chat. Read
[Data and safety](data-and-safety.md) before you enable external integrations or unattended work.
