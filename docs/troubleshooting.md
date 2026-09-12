# Troubleshoot Ricky

Start with the narrowest diagnostic command for the failing feature.

## Ricky cannot find configuration

Ricky reads the installation `ricky.toml` from `user_data_dir` and profile configuration from
`profiles/<name>/ricky.toml` and `.secrets.toml`, regardless
of the current project. The XDG bootstrap pointer selects that root; choose a custom one with
`ricky init --user-data-dir PATH` before the pointer exists. After initialization, a conflicting
`RICKY_USER_DATA_DIR` value fails instead of switching roots.

Check the resolved paths:

```bash
ricky config
```

## A lifecycle command cannot acquire the installation lock

Chat, the gateway, and other stateful commands hold a shared installation lock while
running. Profile lifecycle commands and upgrades need exclusive access. A lifecycle
command launched from inside the running agent can therefore time out waiting for its
parent runtime. Exit chat or stop the affected Ricky process, then run the command in
your terminal. Upgrade can stop an exactly matched managed gateway as part of its own
documented apply sequence. Do not delete a lock file to force access.

## A model provider is not ready

For OpenRouter or Anthropic, confirm that the corresponding key reports `set`:

```bash
ricky config
```

For Claude Code, confirm that the configured executable is available and logged in:

```bash
claude --version
```

Ricky does not silently fall back to another provider.

If a model name is invalid or unavailable, rerun:

```bash
ricky config model
```

## A tool is missing from chat

Optional toolpacks load only when their required configuration is present.

- Slack requires `slack_user_token`.
- Gmail and Google Calendar tools load when an accessible Google account has matching OAuth
  client credentials. Using them also requires an authorization token with the service scopes.
- Web search requires `brave_search_api_key`.
- Memory and workflows must be enabled in the resolved profile scope. A profile can disable
  either even when it is enabled in the installation configuration.
- Browser tools require browser enablement and a supported runtime. See [Browser control](browser-control.md).

Run the integration check described in [Integrations](integrations.md).

## A skill or workflow does not load

Ricky prints bundle load errors when chat starts. You can also run:

```bash
ricky workflow list
ricky workflow validate <name>
```

Check that the bundle directory matches the declared name and that the runtime scope includes its
owning profile. Use a qualified name such as `work/inbox-triage` when multiple accessible profiles
contain the same local name. Put user bundles below `<user_data_dir>/profiles/<profile>/`; project directories are not
discovery roots. Restart chat after adding or editing a bundle.

## A long chat exceeds its context budget

Inspect the context:

```text
/context
```

Then compact older history:

```text
/compact Preserve decisions and unfinished work.
```

Large tool results are normally stored as session artifacts with only a bounded excerpt sent back
to the model. If offloading fails, Ricky reports the failure instead of pretending the complete
result was included.

## A permission keeps appearing

Use `/permissions` to see active grants. A grant might intentionally cover one path, account, or
target rather than every call to the tool. Choose an offered broader grant only when that scope is
appropriate.

Use `/permissions clear` to remove all session grants and review calls individually again.

## A browser resource will not open

List configured resources and check the exact qualified identity:

```bash
ricky browser resources --profile personal
ricky browser check personal/ricky-personal
```

`resource_busy` means another Ricky runtime holds the resource lease. Close that browser session
or the owning Ricky process. A leftover lock file after a crash is inert; POSIX releases the live
lock automatically.

For a headed browser launched from a long-lived tmux server, create a fresh pane after the desktop
session has set `DISPLAY` or `WAYLAND_DISPLAY`. For CDP attachment, confirm that the dedicated
Chrome process is still listening on the configured loopback port. Ricky supports only exact
loopback HTTP endpoints and does not start, stop, or repair an external CDP browser.

If Chrome is missing, install Google Chrome Stable through the host's package manager or Google's
installer, then run `ricky browser status`. Ricky has no browser installer or bundled fallback.
Use `browser.executable_path` for an absolute nonstandard Stable executable. A successful version
check does not prove that Chrome can launch: verify display availability, host sandbox support,
and enterprise automation policies separately. Use a dedicated non-default user-data directory
for external CDP resources.

Chrome branding does not guarantee that automated sessions pass verification. Setup runs ordinary
Chrome without automation attached; controlled browsing can still encounter site restrictions.
Local handoff is available only in Ricky-owned visible sessions. Autonomous challenge recovery and
remote takeover are outside the current browser feature set.

## A browser file or visual operation fails

`screenshot_denied` means the browser resource owner's profile does not list the pinned provider
in `browser.screenshot_allowed_providers`. Add the provider only if masked page pixels are
appropriate for every model you may select through that provider. The allowlist does not test the
exact model's capabilities. Use `/model` to inspect the model pinned to the current chat and verify
its image-input support in the provider's current catalog. If needed, exit and start a new chat
after running `ricky config model --profile PROFILE_NAME`, or pass an image-capable model
with `--provider` and `--model`. Ricky does not switch models automatically.

`screenshot_too_large` means the viewport PNG exceeds an installation screenshot ceiling. Resize
the browser window, reduce page zoom or content, or deliberately revise the applicable browser and
context-media limits. `stale_target` from a coordinate commit means the viewport, scroll position,
navigation, or recaptured masked pixels changed after capture. Request a new visual snapshot; do
not retry the old coordinate.

Browser downloads require a Ricky-owned ephemeral or persistent session. They are intentionally
unavailable over CDP attachment. `download_too_large` means no durable file was published;
`download_publish_failed` can also mean the plain destination filename already exists. Inspect the
owning profile's `downloads/browser` directory before trying again. A missing or tampered logical
media or download reference must be reacquired from its producing operation rather than replaced
with a guessed path or ID.

## A protected value is unavailable

Confirm that `[protected_values] enabled = true` is in the installation `ricky.toml`, then inspect
the owning profile without starting a provider:

```bash
ricky protected-values status --profile personal
ricky protected-values show personal/example-login
ricky protected-values policy show personal/example-login
```

A locked vault prompts only in an interactive foreground terminal. A gateway can receive the key
only through a local `gateway run|service start|service restart --unlock-vault PROFILE` startup;
it never prompts through a message or during an execution. Workflow, named job, scheduled, and
other background runtimes fail closed instead of prompting. A wrong passphrase, copied profile
vault, corrupt payload, or unknown schema version is not repaired automatically.

`protected-value destination policy denied use` means the exact top-level or target-frame HTTPS
origin is outside policy. Inspect approvals and take a new browser snapshot after navigation,
resource update, or approval revocation. Plain HTTP is never accepted for protected material,
even when ordinary browser policy allows that site.

If a gateway execution is awaiting a browser approval, use `/status` in the same authenticated
conversation and copy the exact `/approve` or `/deny` command from the approval message. A bare
`yes` is ignored for this purpose. Expired approvals, restarted gateways, lost browsers, changed
pages, and mismatched one-time codes cannot be repaired or rebound; let Ricky prepare a new live
occurrence. Reconcile an `in_doubt` transaction only after inspecting the external site or account.

## Gateway or messaging is unhealthy

Run offline diagnostics before making network calls:

```bash
ricky gateway doctor
ricky gateway status
```

Then inspect the transport:

```bash
ricky gateway transport doctor telegram personal/bot
```

Use `gateway recover` without `--apply` to preview interrupted-state recovery. See
[Operations](operations.md) before applying recovery or retention changes.

## An upgrade check or apply fails

`ricky upgrade --check` contacts public GitHub Releases anonymously. Confirm
network and DNS access, then retry. Ricky refuses prereleases, downgrades,
malformed release metadata, unexpected redirect hosts, checksum or wheel
version mismatches, an unsupported data generation, and an older-than-required
`uv`. Do not bypass these checks by installing the target wheel manually over
an active migration.

If apply says it requires a uv-tool-installed release, you are running a source
checkout, an unrecognized wrapper, or a different console script. A checkout
can run `ricky upgrade --check`, but apply and recovery must run through the
exact released `ricky` executable installed by `uv tool`.

If Ricky refuses the gateway unit, inspect it with:

```bash
ricky gateway service status
```

Upgrade controls only a marked unit whose launch command binds the exact
installed Ricky executable. Reinstall an owned but stale unit with the current
gateway service command. Move a foreign unit aside only after reviewing who
owns it; Ricky will not overwrite it.

If the installation reports `prepared`, `software_replaced`, `migrating`,
`failed`, or `rolling_back`, ordinary stateful commands are intentionally
blocked. Do not delete `<user_data_dir>/upgrades/` or edit
`installation.json`. Use one of the supported recovery paths:

```bash
ricky upgrade --resume
ricky upgrade --rollback --yes
```

Resume keeps the exact journaled target. Rollback is available only before the
commit fence and requires the matching verified backup and source release
artifacts. After a rollback starts, resume refuses to move forward again,
because the data may already be partly restored. Finish that operation with
`ricky upgrade --rollback --yes`. If a completed upgrade reports that the gateway failed to restart,
the software and data may still be healthy; inspect `ricky gateway status` and
the user-service logs before starting it again.

An `approval_required`, `validation_required`, `lineage_required`, or
`unavailable` schedule is intentionally absent from managed cron. Inspect and
repair or approve it through the ordinary `ricky schedule` commands, then run
`ricky schedule sync`. `--update-jobs` cannot approve expanded unattended
authority, and Ricky never repairs an authored job by rewriting it.

## A schedule reports `lineage_required` after a workflow became bundled

A job records the qualified identity of the workflow it runs. A workflow that
moves from a profile root into the resources bundled with Ricky keeps its
content but changes identity to `bundled/<name>`, which changes the job's spec
digest. The schedule then refuses to run against a definition you have not
reviewed.

Confirm the cause, then record a context decision in the job bundle:

```bash
ricky schedule doctor
ricky job show <job-name>
```

Increment `context.revision` to keep prior run conclusions, or increment
`context.lineage` and reset `context.revision` to `1` to start fresh. Then
re-approve and reinstall:

```bash
ricky schedule approve SCHEDULE_ID
ricky schedule sync
```

## A command or option differs from these docs

Use the installed CLI help as the source of truth for your version:

```bash
ricky --help
ricky <command> --help
ricky <command> <subcommand> --help
```
