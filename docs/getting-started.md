# Get started with Ricky

This guide installs a released Ricky wheel, initializes its private data root,
configures one model provider, and starts your first agent chat.

## Before you begin

The initial released installation supports common Linux systems. You need:

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- access to OpenRouter, Anthropic, or a signed-in local Claude Code CLI

Optional service, schedule, and browser features have additional host
requirements. Basic initialization does not install system packages or contact
the network.

## Install Ricky

Download the wheel for an exact version from the project's GitHub Release and
install it as a `uv` tool. For example, replace `X.Y.Z` with the release
version:

```bash
uv tool install \
  https://github.com/alxjmyr/ricky.sh/releases/download/vX.Y.Z/ricky-X.Y.Z-py3-none-any.whl
```

Confirm that the installed command is available:

```bash
ricky --version
ricky --help
```

Pinning the release URL makes the installed version explicit and reproducible.
`uv` owns the executable and its Python environment; Ricky does not put
application code in its data directory.

## Initialize the data directory

Run the offline, noninteractive initializer:

```bash
ricky init
```

By default, Ricky creates `~/.ricky`. To choose a custom root on the first run,
pass an absolute or home-relative path:

```bash
ricky init --user-data-dir /private/path/to/ricky-data
```

Ricky records that choice in
`${XDG_CONFIG_HOME:-~/.config}/ricky/bootstrap.toml`. Future commands find the
same root automatically. The binding is intentionally immutable: a conflicting
`--user-data-dir` or `RICKY_USER_DATA_DIR` value fails instead of switching
installations.

Initialization creates only `installation.json`, a minimal `ricky.toml`, and
`profiles/shared/`. It enables `shared` as the default profile. It does not
create secret files, download a browser, configure services, or enable optional
features.

Running `ricky init` again verifies the same installation without overwriting
it. Ricky refuses an unknown nonempty target or a partial or mismatched Ricky
installation.

## Configure a model provider

Run the interactive local setup:

```bash
ricky setup
```

Setup lists locally available providers, saves an OpenRouter or Anthropic API
key through a hidden prompt when needed, and selects a model for the `shared`
profile. It does not contact the provider. For Claude Code, the `claude`
executable must already be available on `PATH` and authenticated.

Because `shared` is available to every current and future profile, a credential
stored there is intentionally universal. If the credential should not be
shared, cancel setup and create a narrower profile first:

```bash
ricky profile add personal
```

Then configure that profile as described in [Profiles](profiles.md). Never put
an API key in the installation `ricky.toml`.

You can later change the model with:

```bash
ricky config model --profile shared
```

You can also pin a provider and model for one command:

```bash
ricky ask --provider claude_code --model sonnet "Reply with: Ricky is ready."
```

Use `openrouter`, `anthropic`, or `claude_code` as the provider name.

## Check your configuration

```bash
ricky config
```

Ricky shows resolved paths, provider settings, and whether each secret is set.
It never prints a configured secret value.

See [Profiles](profiles.md) for data separation and
[Configuration](configuration.md) for file precedence and bootstrap behavior.

## Start an agent chat

Run:

```bash
ricky chat
```

You can also run `ricky` with no command. Try a read-only request first:

```text
Summarize the purpose of this repository and cite the files you inspected.
```

Then try a change that exercises the permission system:

```text
Create a file named hello-ricky.md with a one-sentence greeting.
```

Review the permission prompt before you allow the write. Enter `/help` to list
chat commands and `/quit` to exit.

Use `ricky ask` when you want one model response without agent tools, memory,
skills, or a session:

```bash
ricky ask "Explain the difference between an AI agent and a chatbot in three sentences."
```

## Add optional capabilities

Optional components require separate configuration. Browser control uses host-installed Google
Chrome Stable. Install Chrome externally, then check it with `ricky browser status`; Ricky never
downloads a separate browser.

Continue with the feature you need:

- [Configure external integrations](integrations.md)
- [Separate work with profiles](profiles.md)
- [Use memory](memory.md)
- [Use or create skills](skills.md)
- [Run a workflow](workflows.md)
- [Track durable tasks](durable-tasks.md)
- [Run jobs and schedules](jobs-and-schedules.md)
- [Use protected values](protected-values.md)
- [Configure messaging and the gateway](messaging-and-gateway.md)

If a command fails, see [Troubleshooting](troubleshooting.md).

## Upgrade a released installation

Check GitHub Releases for a later stable version:

```bash
ricky upgrade --check
```

Unlike `ricky init`, an upgrade check and apply use the network. Apply an
interactive upgrade with `ricky upgrade`; Ricky shows the exact software and
data-generation change, data root, targeted-backup estimate, profile-job
policy, and possible gateway interruption before asking for confirmation.
For unattended use, pin the exact stable version:

```bash
ricky upgrade --to X.Y.Z --yes
```

`uv` continues to own Ricky's executable and Python environment. Ricky
coordinates the exact `uv tool` replacement but stores no application code in
`user_data_dir`. It verifies release metadata, wheel and constraints digests,
the installed executable, mutable state, and the resulting installation.

If a release includes a deterministic profile-job format update, opt in with
`--update-jobs`. This flag may refresh schedules whose authority is equal or
narrower; it never approves expanded unattended authority. Ricky never
rewrites an authored job bundle. A schedule that needs validation, a project
update, or approval is omitted from managed cron until you resolve it and run
`ricky schedule sync`.

An active Ricky-owned gateway is stopped during apply and restarted only after
the exclusive upgrade operation finishes. If an upgrade is interrupted or
fails, ordinary stateful commands remain gated. Use only the journaled recovery
paths:

```bash
ricky upgrade --resume
ricky upgrade --rollback --yes
```

See [Operations](operations.md#upgrade-ricky) for backup scope, recovery, and
schedule outcomes.

## Remove Ricky

First remove Ricky-managed persistent launch surfaces while the executable is
still available:

```bash
ricky decommission
```

Then remove the software:

```bash
uv tool uninstall ricky
```

These commands preserve the bootstrap pointer and all user data. If you intend
to delete all Ricky data permanently, run `ricky data purge` before uninstalling
the software. See [Operate persistent Ricky services](operations.md) for the
guarded removal sequence.
