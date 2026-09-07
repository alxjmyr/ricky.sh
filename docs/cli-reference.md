# Ricky CLI reference

Ricky resolves one initialized `user_data_dir` through its fixed XDG bootstrap
pointer. Skills, workflows, and jobs come from your profile roots and from the
resources bundled with Ricky; the working directory never selects them. Chat uses the current project for relative file paths and automatic read permission.
Commands that accept `--project`, such as job and schedule commands, use it to
select that working directory. Host paths outside it require the applicable permissions.

```text
ricky [--version] [COMMAND]
```

Running `ricky` without a command starts interactive chat.

## Installation lifecycle commands

| Command | Purpose |
|---|---|
| `ricky init [--user-data-dir PATH] [--json]` | Create or idempotently verify the minimal shared-only data scaffold and bootstrap pointer. |
| `ricky setup` | Interactively configure the shared profile's provider credential and model using local checks only. |
| `ricky upgrade --check [--to VERSION] [--json]` | Query stable GitHub Releases and report compatibility without changing the installation. |
| `ricky upgrade [--to VERSION] [--yes] [--update-jobs] [--json]` | Apply a verified released upgrade with targeted backup, migration, and managed-launch reconciliation. |
| `ricky upgrade --resume [--json]` | Resume the exact active journal after an interruption or failure. |
| `ricky upgrade --rollback --yes [--json]` | Restore the verified pre-upgrade software and declared mutable data before the commit fence. |
| `ricky decommission [--json]` | Stop and remove Ricky-owned gateway service and managed schedules while preserving software, data, and the pointer. |
| `ricky data purge [--yes --installation-id ID] [--json]` | Irreversibly remove the exact inactive initialized data root and its matching pointer. |
| `ricky profile add NAME [--json]` | Create and enable one minimal private profile scaffold without changing the default. |
| `ricky profile set-default NAME [--json]` | Select an existing enabled profile as the installation default. |
| `ricky profile delete NAME [--new-default NAME] [--yes] [--json]` | Confirm and delete one unreferenced non-shared profile and all data below its profile root. |

`init` is offline and noninteractive. Before a pointer exists, an explicit
path takes precedence over `RICKY_USER_DATA_DIR`, followed by the `~/.ricky`
default. After initialization, the pointer is authoritative and conflicting
root selections fail. Unknown nonempty, partial, symlinked, or
identity-mismatched targets are never adopted.

`setup` requires an interactive terminal. It can collect an API key with a
hidden prompt, checks Claude Code availability locally, and saves a shared
profile model default. It does not contact a provider; use `ricky ask` when you
are ready to make a real verification request.

`upgrade --check` contacts the public GitHub Releases API anonymously. Without
`--to`, check and interactive apply select the latest stable release. Apply is
supported only when the running command is the exact console script in a
conforming `uv tool` installation; a development checkout can check releases
but cannot replace software. Versions use strict `MAJOR.MINOR.PATCH` form.
Prereleases and downgrades are refused.

Interactive apply previews its exact target and asks for confirmation.
Unattended and JSON apply require both `--yes` and an exact `--to VERSION`.
`--update-jobs` is valid only when starting a new apply. It permits available
deterministic profile-job updates and safe schedule refresh; it does not grant
authority, rewrite authored jobs, change schedule timing, or answer an approval
prompt. `--resume` reuses the immutable journaled target and choices.
`--rollback` requires `--yes` and is available only before the commit fence.
JSON mode emits one result or error document, including separate managed
gateway and schedule outcomes after a completed recovery.

Interactive `data purge` displays the canonical root and installation ID and
defaults to cancellation. Unattended purge requires both `--yes` and the exact
`--installation-id`, which `--json` also requires so its output stays
machine-readable. Purge refuses while the gateway lock is active, while a
Ricky-owned service unit is installed, or while Ricky's managed crontab block is
installed. Run `ricky decommission` first to remove those launch surfaces.
`uv tool uninstall ricky` removes only the software and preserves all Ricky
data.

Profile lifecycle commands hold the exclusive installation lock. `profile add`
refuses reserved names, duplicate registry entries, and existing unregistered
paths. `profile set-default` accepts any existing enabled profile, including
`shared`; selecting the current default leaves configuration unchanged.
`profile delete` defaults to cancellation, requires `--yes` when
unattended or using JSON output, and requires `--new-default` when deleting the
current default. It reports and refuses configured messaging, gateway,
authority, or schedule references instead of rewriting them. Central durable
history retains its original profile labels and is not promoted or relabeled.

## Everyday commands

| Command | Purpose |
|---|---|
| `ricky ask PROMPT` | Send one direct model request without agent tools. |
| `ricky chat` | Start an ephemeral full agent chat. |
| `ricky config` | Show resolved, redacted configuration. |
| `ricky config model` | Select and save a default provider and model. |
| `ricky config memory` | Show memory routing and note counts. |
| `ricky config slack` | Verify Slack authentication for every configured profile. |
| `ricky config google` | Show Google OAuth status. |
| `ricky config google add NAME --profile PROFILE --email EMAIL --client-json PATH` | Create a profile-owned account definition and import its Desktop OAuth client credentials. |
| `ricky config google auth ACCOUNT` | Authorize one named Google account. |
| `ricky config gmail` | Verify configured Gmail accounts. |
| `ricky config gcal` | Verify configured Calendar accounts. |

`ask` and `chat` accept `--provider/-p` and `--model/-m`. `ask` also accepts `--temperature` and
`--max-tokens`; the Claude Code adapter ignores those two sampling options because its CLI has no
equivalent fields.

## Feature command groups

| Group | Subcommands |
|---|---|
| `profile` | `add`, `set-default`, `delete` |
| `workflow` | `list`, `validate`, `show`, `run`, `status`, `resume`, `abandon`, `reconcile`, `dryrun` |
| `task` | `list`, `show`, `activity`, `artifacts`, `create`, `tag`, `complete`, `cancel`, `reopen` |
| `job` | `list`, `validate`, `show`, `run`, `once`, `history`, `report`, `action` |
| `schedule` | `list`, `show`, `add`, `set`, `enable`, `disable`, `remove`, `refresh`, `approve`, `sync`, `doctor`, `uninstall` |
| `session` | `list`, `show`, `resume`, `archive` |
| `gateway` | `status`, `doctor`, `recover`, `prune`, `audit`, `run`, `process`, `transport`, `inbox`, `service` |
| `capability` | `list`, `show`, `validate` |
| `browser` | `install`, `status`, `resources`, `setup`, `check`, `reset` |
| `protected-values` | `init`, `status`, `rotate-passphrase`, `list`, `show`, `add`, `update`, `disable`, `delete`, `policy`, `approvals` |
| `notification` | `list`, `show`, `retry`, `resolve`, `cancel` |
| `execution` | `list`, `show`, `cancel`, `retry`, `worker`, `draft`, `contract` |
| `authority` | `list`, `show`, `activity`, `revoke` |

Read the corresponding feature guide before using a state-changing or unattended command.

## Browser maintenance commands

| Command | Behavior |
|---|---|
| `ricky browser install` | Explicitly download the Chromium build matched to Playwright and verify its executable. This does not enable browser control. Exits nonzero if installation or verification fails. |
| `ricky browser status` | Report enablement, headed or headless mode, binary location, and Chromium readiness without launching Chromium or downloading files. Exits `0` when the locked executable is ready, even if browser control is disabled; exits `1` when it is missing or readiness inspection fails. |
| `ricky browser resources` | List safe metadata for configured resources in an issued profile scope. Repeat `--access-profile` to include another accessible profile. Endpoints and filesystem paths are omitted. |
| `ricky browser setup PROFILE/NAME` | Open one persistent resource headed for local sign-in or configuration. No model provider or browser snapshot is used. The terminal wait is interruptible and cleanup completes before exit. |
| `ricky browser check PROFILE/NAME` | Open and close one configured resource to verify availability. CDP checks use the configured attachment deadline and disconnect without printing tab content. |
| `ricky browser reset PROFILE/NAME` | Confirm and delete only one idle persistent resource's browser state. Use `--yes` to supply confirmation non-interactively. |

## Protected-value commands

Protected-value lifecycle commands do not construct a model provider. `status`, `list`, `show`,
`policy show`, and `approvals list` expose safe metadata only. Mutation, passphrase, and stored-field
entry require a local interactive terminal; passphrases and raw values have no command-line
options. Run `ricky protected-values --help` for the complete tree and see
[Protected values](protected-values.md) before creating a vault.

## Gateway vault startup options

These gateway commands accept a repeatable `--unlock-vault PROFILE` option:

| Command | Behavior |
|---|---|
| `ricky gateway run --unlock-vault personal` | Take the gateway lock, prompt locally, and keep the selected vault unlocked until this foreground gateway exits. |
| `ricky gateway service start --unlock-vault personal` | Reject an active service before prompting, then unlock the selected vault in the exact new managed process. |
| `ricky gateway service restart --unlock-vault personal` | Prompt locally and unlock the selected vault in the replacement managed process. |

Repeat the option to name multiple profiles. All requested vaults must be enabled, initialized, and
successfully unlocked or gateway startup fails with the complete set locked. Omitting the option
starts the gateway normally with its vaults locked. A later automatic restart also starts locked.

The option takes a safe profile name, never a passphrase. Passphrases come only from no-echo local
prompts and do not enter command arguments or environment variables. See
[Protected values](protected-values.md) for lifecycle and security details.

Browser transaction approval commands are messages sent inside the authenticated gateway
conversation, not host CLI commands. Use `/approve APPROVAL CODE`, `/deny APPROVAL CODE`,
`/status`, `/cancel execution_<id>`, and
`/reconcile browser_transaction_<id> performed|not_performed NOTE` as documented in
[Messaging and gateway](messaging-and-gateway.md).

## Get exact help

The CLI help for your installed version is the source of truth for arguments,
accepted values, and defaults:

```bash
ricky --help
ricky workflow --help
ricky workflow run --help
```

Use `--help` before you script a command. Ricky is under active development and advanced command
surfaces can change between versions.
