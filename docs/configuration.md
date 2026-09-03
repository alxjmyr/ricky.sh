# Configure Ricky

Ricky loads user-global configuration from one initialized `user_data_dir`. The
default root is `~/.ricky`. On first initialization, `--user-data-dir` or
`RICKY_USER_DATA_DIR` can choose a different root. Ricky then records the
canonical path in its fixed XDG bootstrap pointer and uses it for later
commands.

## Use the correct configuration file

| File or directory | Put this content here | Commit it? |
|---|---|---|
| `${XDG_CONFIG_HOME:-~/.config}/ricky/bootstrap.toml` | Ricky-managed pointer to the initialized root and installation ID | No |
| `<user_data_dir>/installation.json` | Ricky-managed installation identity and format metadata | No |
| `<user_data_dir>/ricky.toml` | Profile registry and installation-wide process/storage settings | No |
| `<user_data_dir>/profiles/<name>/ricky.toml` | Profile defaults, policy, accounts, and integration settings | No |
| `<user_data_dir>/profiles/<name>/.secrets.toml` | Credentials owned by that profile | No |
| `ricky.toml.example` and `.secrets.toml.example` | Authored configuration and credential references | Yes |
| `.ricky/` | Project-local directory Ricky treats as private; no resources are discovered from it | Yes |
| `user_data_dir` | Live configuration, profile data, generated state, tokens, sessions, and artifacts | No |

Create the minimal scaffold before editing configuration:

```bash
ricky init
```

Initialization enables only `shared` and makes it the default. It does not
create a secrets file. Create `.secrets.toml` under the profile that owns a
credential only when you need to save that credential, and restrict it to the
current user. `ricky setup` can securely prompt for the initial shared provider
credential and model without contacting the provider.

Never put a credential in `ricky.toml` or project `.ricky/`. Restrict access to the live user-data
root. See [Profiles](profiles.md) before sharing a credential across contexts.

## Inspect resolved settings

```bash
ricky config
```

This command reports configuration paths and whether secrets are set. It does not print secret
values.

## Understand precedence

Explicit programmatic overrides take precedence over files. Within one profile, its `.secrets.toml`
overrides its `ricky.toml`. Runtime resolution then applies settings by category:

1. The primary profile supplies ordinary defaults.
2. The shared profile supplies fallback defaults when it is not already the primary.
3. Accessible resource catalogs are combined with qualified identities.
4. Provider and capability restrictions intersect across the complete scope.
5. Safety ceilings use the strictest applicable value.
6. Installation settings and built-in defaults supply the final fallback.

A partial profile section overrides only the keys it contains. Unset sibling keys continue to use
shared, installation, or built-in values. Storage and process mechanics remain installation-owned;
profiles cannot redirect `workflow.run_dir`, `authority.store_path`, other central stores, or poll
intervals.

Ricky does not read settings or credentials from environment variables. Store
configuration in `ricky.toml` and credentials in the owning profile's
`.secrets.toml` so configuration inspection and future configuration editors
share one source of truth. Before a pointer exists, the sole Ricky-specific
bootstrap exception is `RICKY_USER_DATA_DIR`. After initialization, the pointer
is authoritative and a conflicting value fails. `XDG_CONFIG_HOME` selects the
host configuration directory containing the pointer; it is not a Ricky
setting.

## Select a provider and model

Use the guided picker:

```bash
ricky config model --profile shared
```

Or edit the profile definition in `<user_data_dir>/profiles/shared/ricky.toml`:

```toml
[profile]
default_provider = "anthropic"

[profile.default_models]
anthropic = "claude-sonnet-5"
```

Available provider names are:

- `openrouter`
- `anthropic`
- `claude_code`

The picker offers only providers allowed by the target profile's complete scope. Its displayed
default and manual-model fallback also come from that profile.

`--provider` and `--model` override these defaults for one `ask` or `chat` invocation.

## Understand the data roots

The default user-data root is `~/.ricky`. The project-local private root
defaults to:

```toml
project_data_dir = ".ricky"
```

`user_data_dir` owns live configuration, profile data, and generated runtime
state. Choose a non-default value only during the first `ricky init`. The XDG
bootstrap pointer is the sole persisted authority for that root;
`<user_data_dir>/ricky.toml` does not repeat or redirect it. Relocating or
repointing an initialized root is not supported.

`project_data_dir` names a project-local directory that Ricky treats as its own
private state: it refuses to attach files from it. Ricky discovers no skills,
workflows, or jobs from a project directory. Do not point both settings at the
same directory.

## Set the timezone

Set an IANA timezone name:

```toml
user_timezone = "America/Chicago"
```

New sessions use this timezone for time-sensitive reasoning and scheduling defaults.

## Configure optional feature groups

The included `ricky.toml.example` documents every supported setting and its default. Common sections
are:

| Section | Controls |
|---|---|
| `[context]` | Context estimation, output reserve, large tool results, and manual compaction |
| `[memory]` | Profile memory enablement and note limits |
| `[workflow]` | Workflow enablement, parallelism, retries, and data limits |
| `[sessions]` | Persistent conversation storage, turn wall time, and retention |
| `[jobs]` | Job bundle and run storage |
| `[schedules]` | Managed cron launcher limits |
| `[messaging]` | Notification storage, transports, routes, and attachments |
| `[gateway]` | Persistent gateway runtime, routes, recovery, retention, and service settings |
| `[agents.*]` | Standing capability eligibility for foreground and background agents |
| `[executions]` | Durable background request limits and worker settings |
| `[authority]` | Task-scoped delegation ceilings; disabled by default |
| `[browser]` | Explicit Chromium installation, runtime limits, and destination policy |
| `[protected_values]` | Encrypted profile-vault mechanics, limits, and prompt timeout |

Follow the relevant feature guide before you change an advanced section. Ricky validates typed
settings at startup and fails instead of silently accepting an invalid value.

`sessions.turn_wall_seconds` must be no greater than 3570 seconds so the gateway can retain 30
seconds of inbox-claim headroom. `gateway.service.unit_dir` defaults to the XDG systemd user-unit
directory and can be set to an absolute or `~` home-relative path.

Browser mechanics and limits are installation-owned. Profiles cannot redirect the Chromium binary
directory, browser-state directories, download directory, or relax runtime limits.
`browser.binary_dir` is relative to `user_data_dir`; `browser.ephemeral_dir`,
`browser.persistent_dir`, `browser.lease_dir`, and `browser.download_dir` resolve below the profile
that owns each session or configured resource.

The file and visual ceilings are:

| Setting | Default |
|---|---:|
| `browser.upload_count_limit` | 10 |
| `browser.upload_file_byte_limit` | 20,000,000 bytes |
| `browser.upload_total_byte_limit` | 50,000,000 bytes |
| `browser.download_file_byte_limit` | 50,000,000 bytes |
| `browser.visual_candidate_limit` | 100 |
| `browser.screenshot_width_limit` | 2,000 pixels |
| `browser.screenshot_height_limit` | 2,000 pixels |
| `browser.screenshot_pixel_limit` | 4,000,000 pixels |
| `browser.screenshot_file_byte_limit` | 5,000,000 bytes |

`[browser.background]` is a separate disabled-by-default owner boundary. `enabled` must be true
before any feature switch. `read_enabled` admits guarded browsing; `interaction_enabled`,
`protected_values_enabled`, and `commit_enabled` progressively admit their narrower surfaces.
`allow_ephemeral` and `allow_public_https_research` control the launch and destination ceilings.
`[browser.background.budget]` bounds session starts, navigations, scrolls, pages, semantic and
visual observations, interactions, protected materializations, file operations and bytes,
transaction attempts, parked browsers, and approval lifetime. These values are installation
ceilings; a compiled execution or named job can only narrow them. A background upload also requires
an exact in-scope durable-task artifact id and a contract-pinned digest and size; these settings do
not authorize arbitrary host paths.

Gateway effects also require `[authority]` and the separately enabled
`authority.capabilities.browser_interact`, `protected_value_use`, and `browser_commit` tables.
`browser_commit.max_financial_limit_minor` and `currency` form the owner spend ceiling. A
transaction still requires an exact per-occurrence approval; the ceiling is not standing approval
to spend up to that amount. See the complete disabled example in `ricky.toml.example`.

Profile `ricky.toml` files may define non-secret `[browser.resources.<name>]` entries of kind
`persistent` or `cdp`. Persistent paths are derived by Ricky; CDP endpoints must be exact loopback
HTTP endpoints with a port. A profile-local `[browser]` section owns
`screenshot_allowed_providers`; its empty default denies screenshot capture for resources owned by
that profile. Listing a provider is a standing disclosure decision for read-only visual snapshots,
not a per-capture prompt or an exact-model capability check. The chat's pinned provider must be
listed, and its pinned model must independently accept image input. To allow a private or
special-network destination, add its exact HTTP or HTTPS origin to installation-owned
`browser.allowed_private_origins`; do not include a path, query, fragment, or credentials. See
[Browser control](browser-control.md) before enabling this feature.

Protected-value mechanics are also installation-owned. Set `protected_values.enabled = true` to
compose the safe catalog and foreground browser consumer. `protected_values.dir` resolves below
each owning profile; it never resolves below project `.ricky/`. The KDF defaults are
`argon2_iterations = 3`, `argon2_lanes = 4`, and `argon2_memory_kib = 65536`. Catalog, audit,
SQLite wait, and secure-prompt ceilings are documented in `ricky.toml.example`. Each unlock slot
retains the KDF parameters used to create it; changing these settings affects newly initialized
vaults and the next passphrase rotation, not how an existing slot is unlocked. Do not put a vault
passphrase or agent-usable protected value in `ricky.toml`, `.secrets.toml`, or an environment
variable. See [Protected values](protected-values.md).

Gateway vault unlock is an explicit startup action, not a configuration or secret setting. Use a
repeatable `--unlock-vault PROFILE` option with `gateway run`, `gateway service start`, or
`gateway service restart`. Do not add a vault passphrase to `.secrets.toml` or an environment
variable. Omitting the option starts the gateway locked, and an automatic service restart does not
retain prior unlocked state.

`[context.media]` is the provider-neutral session-media policy. Its defaults allow 25,000,000
stored bytes per session and project at most two images, 10,000,000 source bytes, and 8,000,000
pixels into one request. Each projected image reserves 8,192 estimated tokens by default. Set
`image_token_estimate` on an exact `[[context.models]]` provider/model record when a different
deterministic reserve is appropriate. These ceilings apply to future media producers as well as
browser screenshots.
