# Separate work with profiles

Profiles separate persona, configuration, memory, capabilities, accounts, credentials, tasks,
downloads, temporary files, and other user data. `shared` is a reserved profile included in every
runtime. Put data there only when every Ricky context should be able to use it.

## Choose a runtime scope

Ricky starts commands with `shared` and the configured default profile. A new
installation enables only `shared`, so it is initially both the universal and
primary profile. Select another enabled primary profile with `--profile`:

```bash
ricky chat --profile work
ricky ask --profile personal "Summarize my open commitments."
```

Add one or more readable profiles with repeatable `--access-profile` options:

```bash
ricky chat \
  --profile personal \
  --access-profile work
```

The primary profile supplies ordinary defaults, persona, and the default
destination for new user data. When `shared` is primary, its settings and
persona are applied once. Added profiles supply resources but do not inject
their persona. The scope is pinned for the session and cannot be widened by
model output, tool arguments, or child work.

Data, credentials, and configuration written to `shared` remain intentionally
available to every future profile. Adding a narrower profile does not move or
relabel existing shared-owned data.

For broad questions such as “what needs my attention?”, Ricky can inspect every task profile in
scope and return labelled results. For an exact write with two materially different plausible
destinations, Ricky asks which profile you mean.

## Configure the profile registry

Initialization creates this minimal registry:

```toml
[profiles]
default = "shared"
enabled = ["shared"]
```

Create a narrower profile with:

```bash
ricky profile add personal
```

The command validates the name, creates an owner-only
`profiles/personal/ricky.toml`, and adds the profile to the installation
registry. It does not change the default profile, create a secrets file, or
pre-create optional runtime directories. Ricky creates those files and
directories through the feature that owns them.

Profile names start with a lowercase letter and contain only lowercase
letters, digits, hyphens, and underscores. They are at most 64 characters.
`shared` and `bundled` are reserved.

Edit the generated registry definition to describe how Ricky should route work
to the new profile. For example:

```toml
[profiles]
default = "personal"
enabled = ["shared", "personal", "work"]

[profiles.definitions.personal]
description = "Personal life and household context."
routing_hints = ["Family, home, health, and personal administration"]
default_provider = "openrouter"
allowed_providers = ["openrouter", "anthropic"]

[profiles.definitions.personal.default_models]
openrouter = "openai/gpt-5.6-luna"

[profiles.definitions.work]
description = "Employer and professional context."
routing_hints = ["Company, clients, and professional projects"]
default_provider = "claude_code"
allowed_providers = ["claude_code"]
```

A multi-profile runtime may use only a provider allowed by every profile in scope.

## Delete a profile

Delete an unused profile and all data it owns with:

```bash
ricky profile delete personal
```

Ricky reports every remaining reference before it asks you to confirm, so a
refused deletion never prompts. Use `--yes` only when supplying that
confirmation noninteractively. If the profile is the current default, select
another enabled profile explicitly:

```bash
ricky profile delete personal --new-default shared
```

Ricky refuses to delete `shared`. It also refuses deletion while a messaging
transport or route, gateway route, delegated-authority ceiling, or desired
schedule still names the profile. Remove or update every reported reference,
then run the command again; Ricky does not guess how security policy or
runnable work should be reassigned.

Deletion reports a reference even when your current profile scope cannot reach
it. If `ricky schedule list` does not show a reported schedule, that schedule
is also pinned to a profile that is no longer enabled. Re-enable that profile,
remove the schedule with `ricky schedule remove`, then delete the profile
again.

Deletion removes the complete profile directory, including its configuration,
credentials, authored resources, and generated profile-owned state. Central
historical records keep their original profile labels and are not relabeled or
promoted to `shared`. After deletion, ordinary runtime scopes cannot access a
record that still requires the removed profile.

## Add profile configuration and secrets

Put environment-specific settings in:

```text
<user_data_dir>/profiles/<name>/ricky.toml
<user_data_dir>/profiles/<name>/.secrets.toml
```

For example, a personal profile can own a Google account:

```toml
# profiles/personal/ricky.toml
[google.accounts.home]
email = "you@example.com"
```

```toml
# profiles/personal/.secrets.toml
[google_oauth_clients.home]
client_id = "...apps.googleusercontent.com"
client_secret = "..."
```

The runtime refers to this resource as `personal/home`. Same-named resources in different profiles
cannot shadow each other.

Installation-wide process and storage mechanics remain in the root `ricky.toml`. Messaging
transports and logical routes are installation-wide, while their account credentials live in the
owning profile.

Agent-usable protected values are a separate category. They live in an encrypted profile-local
vault, use qualified aliases such as `personal/example-login`, and can be materialized only through
a purpose-built local consumer. They are not imported from `.secrets.toml`. Safe aliases and field
labels may reach the model provider. See [Protected values](protected-values.md).

## Set persona instructions

Place persona instructions at:

```text
<user_data_dir>/profiles/shared/SOUL.md
<user_data_dir>/profiles/<primary>/SOUL.md
```

Ricky loads the shared persona, followed by the primary persona when the
primary is not `shared`. It does not load persona files from additional
accessible profiles and never injects the shared persona twice.

## Understand storage and derived records

Profile-owned data lives below `<user_data_dir>/profiles/<name>/`. Records that can contain data
from multiple profiles—such as sessions, executions, and notifications—carry a required profile
label. Their owning stores reject a read or mutation unless the caller's scope includes the entire
label.

Ricky reads only the profile-era layout. Development installations created before profiles must be
recreated or repaired manually; no legacy migration command remains in the runtime.
