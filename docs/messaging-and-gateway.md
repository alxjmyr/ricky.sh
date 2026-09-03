# Use messaging and the gateway

The gateway keeps Ricky running, polls configured messaging transports, persists trusted inbound
messages, resumes conversations across restarts, dispatches background executions, and delivers
durable notifications.

Use ordinary terminal [chat](chat.md) if you do not need persistent remote conversations.

## Configure Telegram

Telegram is the currently implemented messaging transport.

Create a bot with BotFather. Put the account configuration and token in its owning profile. For a
personal bot, use `<user_data_dir>/profiles/personal/.secrets.toml`:

```toml
[messaging.telegram_accounts.bot]
bot_token = "BOT_TOKEN"
allowed_sender_ids = ["TELEGRAM_USER_ID"]
allowed_destination_ids = ["TELEGRAM_CHAT_ID"]
enabled = true
```

The non-secret account fields may instead live in that profile's `ricky.toml`; Ricky merges the two
profile documents. Add the installation-wide transport, messaging route, and gateway route to
`<user_data_dir>/ricky.toml`:

```toml
[messaging.transports.telegram-personal]
type = "telegram"
account = "personal/bot"

[messaging.routes.owner]
transport = "telegram-personal"
destination = "TELEGRAM_CHAT_ID"
owner_profile = "personal"
accepted_profiles = ["shared", "personal"]

[gateway]
enabled = true
operator_route = "owner"

[gateway.routes.owner]
provider = "openrouter"
model = "MODEL_ID"
primary_profile = "personal"
access_profiles = []
project_root = "."
```

Transport account references always use the qualified `profile/name` form. Ricky does not accept
an unqualified local name such as `bot`, even when only one profile defines it. The messaging
route's `owner_profile` must match the profile that owns the referenced account.

Use exact decimal Telegram IDs. An empty allowlist rejects every message. `primary_profile` is the
default destination for profile-owned writes in this route. `access_profiles` grants additional
profile access; the shared profile is included in every gateway profile scope.

If you do not know the IDs, leave both allowlists empty, send the bot one private message, and poll
once. Inspect the bounded rejected inbox record to find the exact sender and destination IDs, add
them to the account configuration, and send a fresh message.

`owner_profile` identifies the profile that owns a static outbound messaging route.
`accepted_profiles` is its explicit clearance list. Include `shared` only when the route may carry
shared-profile data. Notifications whose required profile label is not a subset of this list are
rejected before delivery.

Every gateway route requires a same-named messaging route. That messaging route must accept every
profile in the gateway route's `primary_profile` and `access_profiles` scope. Ricky rejects an
invalid pairing at startup, before a foreground turn can run without a deliverable response path.

`project_root` binds the route to that exact project after `~` expansion and relative-path
resolution. Omit it for an intentionally projectless route. A projectless route can still use
capabilities, skills, and jobs owned by profiles in the route's scope, plus the resources bundled
with Ricky, but it does not advertise background capabilities that require a project root.

Gateway conversations cannot start workflows directly. The foreground gateway excludes the
mutating `start_workflow` control, and ad hoc delegated jobs do not load the workflow runner. A
foreground agent can use `start_named_job` to queue an authored workflow-backed job. The
background job resolves omitted arguments and runs the exact workflow under its standing
permissions, budget, overlap lock, and audit chain.

## Validate the setup

Check the Telegram identity, then the complete gateway configuration:

```bash
ricky gateway transport doctor telegram personal/bot
ricky gateway doctor
ricky gateway status
```

`gateway doctor` validates configuration, routes, storage, credentials, capabilities, and
supervision without calling a model.

## Run the gateway

For an initial foreground test:

```bash
ricky gateway run
```

Only one gateway can own a `user_data_dir`. Do not run a foreground gateway while the managed
service is active.

The gateway starts with every protected-value vault locked by default. To make one or more
initialized vaults resident in this gateway process, request each profile explicitly:

```bash
ricky gateway run \
  --unlock-vault personal \
  --unlock-vault work
```

Ricky acquires the gateway lock before showing the local no-echo prompts. A failed prompt or
passphrase prevents startup and leaves the complete requested set locked. The option changes only
vault availability; route scope and capability policy remain authoritative. See
[Protected values](protected-values.md).

Send your bot a plain-text message. Trusted messages enter a persistent conversation and retain
context across gateway restarts. Telegram inbound supports text only; unsupported or untrusted
content is rejected before it can reach a provider or agent runtime.

## Use conversation commands

Send these commands to the bot:

| Command | Action |
|---|---|
| `/help` | Show gateway conversation commands. |
| `/new` | Archive the current session and start a fresh conversation. |
| `/compact` | Compact older persistent context. |
| `/context` | Show the current foreground session's prospective context details. |
| `/status` | Show conversation and background-work status. |
| `/cancel execution_<id>` | Request cancellation of one background execution. |
| `/approve browser_<approval-id> <one-time-code>` | Approve one exact parked browser transaction or protected destination occurrence. |
| `/deny browser_<approval-id> <one-time-code>` | Deny that exact occurrence. |
| `/reconcile browser_transaction_<id> performed\|not_performed <note>` | Append an operator attestation to an uncertain transaction without rewriting its evidence. |

The terminal-chat `/workflow` command is not available in gateway conversations. Ask the gateway
agent to start the exact named job instead.

Foreground replies and agent-generated notifications use mobile-first portable Markdown. Telegram
renders headings, emphasis, lists, task lists, compact tables, quotes, links, code, formulas, and
footnotes. Prefer bullets on a phone when a table would need more than three short columns. Use an
attachment for a large report, dataset, file, or media item.

Ricky removes raw HTML, embedded Markdown media, platform-specific links, mentions, and interactive
controls before delivery. Outbound text is split into independently valid parts below Telegram's
message limit. If Telegram definitively rejects rich formatting, Ricky sends one readable
plain-text projection. It does not fall back or retry when the first send might have succeeded.

Outbound file attachments use the host-file and protected-state rules in
[Integrations](integrations.md). Manually enqueued legacy or operator reply text remains literal
plain text unless its notification contract explicitly selects portable Markdown.

Each conversation pins its provider, model, profile scope, project root, and capability policy. If
that route is removed or any pinned value changes, Ricky fails closed and tells you to send `/new`.
`/new` archives the prior session and starts under the current route configuration; replay after a
gateway restart resumes the same rotation instead of archiving a second conversation.

The profiles routing correction changes the capability-policy digest from process-wide settings to
the route's exact profile scope. After upgrading from the initial profiles release, send `/new`
once in each existing live conversation when Ricky reports route-policy drift.

This release intentionally does not provide aliases for earlier unqualified Telegram account
references. Update each transport from a local name such as `bot` to `profile/bot`. Telegram
authority principals use the same qualified account, for example
`telegram:personal/bot:TELEGRAM_USER_ID`.

Cancellation is durable. A queued execution normally becomes `cancelled` immediately. A running
one may appear as `cancel_requested` in `/status` while its owner stops and joins the work. It ends
as `cancelled` only when no work became observable; otherwise Ricky records `uncertain` rather than
claiming the work stopped cleanly. `/cancel` can control only executions created by that
conversation.

When browser background policy is enabled, the foreground gateway agent can create a durable ad
hoc execution with an exact browser contract. It still has no browser tools itself. The worker can
research public HTTPS sites, interact within the approved scope, and propose a specific financial
or generic browser transaction after it discovers the merchant and current details. You do not
need to predeclare a transaction origin; approval binds the commit to the exact proposed live
origin and occurrence.

A worker that reaches a commit parks its live browser and sends the trusted approval summary. A
bare `yes` cannot approve it. Copy the complete `/approve` or `/deny` command from that message.
Use `/status` to see the execution, approval ID, state, expiry, and review digest. If the live
browser or gateway process is lost, the approval becomes invalid and cannot authorize a rebuilt
session. See [Browser transaction approvals](browser-transactions.md).

Gateway protected fills require the relevant profile vault to have been unlocked locally at
gateway startup. Vault availability does not itself authorize a protected use. Named and
scheduled jobs remain limited to the read-oriented browser surface and cannot borrow resident
gateway vault keys or commit transactions.

Another active `getUpdates` consumer for the same bot causes a Telegram conflict. Stop other bot
pollers before you run Ricky.

## Inspect persistent sessions

Gateway conversations create the sessions managed by these commands:

```bash
ricky session list
ricky session show SESSION_ID
ricky session resume SESSION_ID
ricky session archive SESSION_ID
```

`session resume` provides a terminal interface to one stored conversation. Each message opens a
fresh bounded runtime, and only `/debug` and `/quit` are supported during resume. Permission grants
do not carry between persistent turns.

## Use bounded diagnostics

The normal path is `gateway run`. Use these commands for testing or recovery:

```bash
ricky gateway transport poll telegram personal/bot --once
ricky gateway transport deliver --once
ricky gateway process --once

ricky gateway inbox list
ricky gateway inbox show MESSAGE_ID
ricky gateway inbox reply MESSAGE_ID --text "Reply text"
ricky gateway inbox dismiss MESSAGE_ID
```

Polling persists one inbound batch. Delivery processes one notification batch. Processing handles
one bounded pending-inbox batch. These commands do not replace the long-running gateway.

If an outbound send has no reliable receipt, Ricky records it as in doubt and does not retry it
automatically. Inspect Telegram before you reconcile or retry delivery. See
[Operations](operations.md).

## Run the managed service

On Linux with `systemd --user`, inspect and install the generated unit:

```bash
ricky gateway service show
ricky gateway service install
ricky gateway service start
ricky gateway service status
ricky gateway doctor
```

Install enables the service by default but does not start it. The unit uses the absolute `uv` path
and does not place secrets on the command line.

To start the service with an initialized profile vault unlocked, enter its passphrase locally:

```bash
ricky gateway service start --unlock-vault personal
```

Repeat `--unlock-vault PROFILE` for each required profile. If the service is already active,
`start --unlock-vault` fails before prompting. Replace the process instead:

```bash
ricky gateway service restart --unlock-vault personal
```

The CLI transfers each passphrase through a private, one-use local socket to the exact gateway
process that owns the current gateway lock. A failed handoff or unlock fails startup; passphrases
never enter systemd arguments, environment variables, unit text, logs, configuration, or regular
files.

Vault unlock is process-local. `service restart` without the option deliberately starts locked,
as does an automatic restart after a crash. Run a local `restart --unlock-vault PROFILE` again when
you want the replacement process unlocked.

The unit directory defaults to `$XDG_CONFIG_HOME/systemd/user`, or
`~/.config/systemd/user` when `XDG_CONFIG_HOME` is unset. Override it with a path that resolves to
an absolute directory:

```toml
[gateway.service]
unit_dir = "~/.config/systemd/user"
```

Manage or remove it with:

```bash
ricky gateway service restart
ricky gateway service stop
ricky gateway service uninstall
```

Ricky refuses to overwrite or delete an unmarked unit. `uninstall` stops, disables, and reloads
the user manager even when no unit file is present, because systemd can still hold the unit
loaded after the file is removed by hand. It reports each `systemctl` exit code instead of
failing, so a host without a user session bus can still remove the unit. Gateway service logs
default to `<user_data_dir>/logs/ricky-gateway.log`.
