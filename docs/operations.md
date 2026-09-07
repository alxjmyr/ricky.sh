# Operate persistent Ricky services

This page is for users who run the gateway, notification delivery, or durable background
executions.

## Check gateway state

Start with provider-free checks:

```bash
ricky gateway status
ricky gateway doctor
```

`status` reports durable counts, the gateway lock, transport cursors, uncertain and in-doubt work,
and bounded recent errors. `doctor` validates configuration, schemas, private storage permissions,
routes, capabilities, contract evidence, credentials, the lock, and managed-service drift.

## Recover interrupted work

Preview recovery first:

```bash
ricky gateway recover
```

Apply the displayed recovery plan only after review:

```bash
ricky gateway recover --apply
```

Recovery reclaims a record only when no observable attempt occurred. Work that started but has no
conclusive receipt becomes uncertain or in doubt. Repeating the same recovery is safe; Ricky does
not infer whether an external effect happened.

## Audit one chain of work

```bash
ricky gateway audit CORRELATION_ID
```

Audit follows exact links across inbox messages, conversations, review drafts, confirmations,
contracts, executions, grants, and notification outbox entries. It does not copy transcript bodies
or guess missing relationships.

## Apply retention

Preview exact records and paths:

```bash
ricky gateway prune
```

To apply pruning, first enable and configure `[gateway.retention]` in
`<user_data_dir>/ricky.toml`, then run:

```bash
ricky gateway prune --apply
```

Ricky protects unresolved, open, queued, leased, uncertain, in-doubt, and referenced evidence.
Candidates must also exceed configured age and count thresholds. A dry run is the default.

## Inspect notification delivery

Notification producers create immutable requests and durable outbox entries. The CLI inspects and
reconciles them; it does not provide a general command to create a notification.

```bash
ricky notification list
ricky notification show NOTIFICATION_ID
```

Retry only a failed delivery that you know did not occur:

```bash
ricky notification retry OUTBOX_ID
```

For an in-doubt delivery, inspect the destination and record the observed result:

```bash
ricky notification resolve OUTBOX_ID --as delivered
ricky notification resolve OUTBOX_ID --as not_delivered
```

Resolving as `not_delivered` changes the entry to failed. Run `retry` separately if you want another
attempt. Cancellation cannot undo a delivered message or stop an entry already claimed by a
worker:

```bash
ricky notification cancel OUTBOX_ID
```

A removed, disabled, unsupported, or no-longer-allowed destination is marked `failed` without
blocking later pending deliveries. Inspect the stored error before you repair the route and issue
an explicit retry.

## Inspect background executions

Executions are durable fire-and-report requests normally created through the gateway or agent
tools. There is no CLI creation command.

```bash
ricky execution list
ricky execution show REQUEST_ID
ricky execution cancel REQUEST_ID
ricky execution retry REQUEST_ID
ricky execution worker --once
```

Cancellation is a durable request, not an immediate claim that running work has stopped. Queued or
otherwise unstarted work becomes `cancelled`; running work first becomes `cancel_requested` while
the owning worker cancels and joins it. Ricky records the terminal state as `cancelled` only when
no work became observable. Provider output, a reserved or performed external effect, or ambiguous
finalization instead produces `uncertain`.

The gateway already runs an execution worker. Do not start a second continuous worker unless you
have intentionally designed the deployment for it.

Named-job retries create child attempts. Ad hoc retries require a fresh authenticated proposal;
Ricky does not replay expired confirmation evidence.

Ad hoc executions pin an immutable contract that includes the task revision, complete profile
scope, provider, model, resources, budgets, policy digests, route, guardrails, and confirmations.
Inspect the exact contract and its capability expansion:

```bash
ricky execution contract show CONTRACT_ID_OR_DIGEST
ricky execution contract explain CONTRACT_ID_OR_DIGEST
```

Inspect or cancel an incomplete live-review draft:

```bash
ricky execution draft list
ricky execution draft show DRAFT_ID
ricky execution draft cancel DRAFT_ID
```

Policy drift, expired evidence, or an interrupted started request fails closed. Ricky does not
automatically requeue uncertain execution work.

A browser-bearing execution also owns a fenced browser attempt and durable operation budgets.
`awaiting_transaction_approval` means its worker is still alive and has parked an exact in-memory
commit; approve, deny, inspect, or cancel it through the authenticated gateway conversation.
Reconciliation becomes available only if a consumed transaction ends `uncertain` or `in_doubt`;
it does not apply while approval is still pending. Restart recovery invalidates a parked occurrence
because live browser and prepared effect state are deliberately not durable. Read-only browser
work can start a fresh attempt from safe logical context, but Ricky never reconstructs or replays
an effect that may have become observable.

## Inspect capability policy

```bash
ricky capability list
ricky capability list --agent gateway_foreground
ricky capability show CAPABILITY_ID
ricky capability validate
```

These commands show installed resource mappings, provenance, risk, unattended eligibility,
confirmation requirements, guardrails, and resolved standing policy.

Configure `agents.gateway_foreground` and `agents.ad_hoc_background` independently
in `ricky.toml`. `exclude_capabilities` removes a capability from eligibility;
`confirmation_required_capabilities` requires authenticated confirmation;
`guardrail_required_capabilities` requires a supported, exact constraint before use.
Gateway route settings can narrow these policies further. Use IDs from the inventory,
then run `ricky capability validate` after editing policy.

Empty policy lists do not grant effects. An ad hoc execution still needs an exact
compiled contract, and delegated effects need their enabled authority evaluator.
Set `[agents.ad_hoc_background.execution]` budgets explicitly before delegating effects;
the default external-effect ceiling is zero. Browser owner settings and protected-value
policies impose additional ceilings. Named jobs use their own authored tool and
permission lists, described in [Jobs and schedules](jobs-and-schedules.md).

## Understand delegated-authority support

The `ricky authority` command group can inspect and revoke stored task-scoped grants. Production
browser interaction, protected-value use, and browser commit capabilities provide specialized
evaluators when their owner policy is enabled. Use the capability inventory to see which evaluators and tools are installed.
Inspect and revoke grants with:

```bash
ricky authority list
ricky authority show GRANT_ID
ricky authority activity GRANT_ID
ricky authority revoke GRANT_ID --reason "No longer required"
```

Revocation prevents future calls. It cannot undo an effect that already occurred.

## Upgrade Ricky

Run an anonymous, networked stable-release check first:

```bash
ricky upgrade --check
ricky upgrade --check --to X.Y.Z
```

The check inspects release compatibility and Ricky-owned durable formats but
does not replace software or migrate data. A source checkout may run the check.
Only the exact `ricky` console script from a released `uv tool` installation can
apply or recover an upgrade.

For an interactive apply, run:

```bash
ricky upgrade
```

Ricky shows the selected release, current and target data generations, data
root, migration count, targeted-backup estimate, profile-job update choice, and
gateway interruption before confirmation. For automation, specify both the
exact version and confirmation:

```bash
ricky upgrade --to X.Y.Z --yes
```

The operation downloads and verifies the exact source and target wheel and
constraints artifacts, then records an immutable journal below
`<user_data_dir>/upgrades/<operation_id>/`. It holds an installation-wide
exclusive lock across software replacement, data migration, verification, and
managed-launch reconciliation. Other stateful Ricky commands wait or fail
boundedly during that interval.

### Expect managed-service interruption

If the installed gateway unit is Ricky-owned and binds the exact source
launcher, upgrade records whether it was enabled and active. It stops an active
gateway before taking the exclusive lock, rewrites the unit to the verified
target launcher, preserves enabled state, and restarts it after releasing the
lock. An inactive gateway remains inactive. Ricky refuses a foreign, edited,
or ambiguously bound unit instead of controlling it. A restart failure is
reported separately from a successful software and data result.

Managed cron entries are rendered with the exact released `ricky` launcher.
Ricky preserves desired schedule IDs, cron expressions, enabled state, profile
scope, project roots, and approval evidence. Invalid, unavailable,
lineage-blocked, or approval-blocked schedules remain in desired state but are
omitted from the installed managed block.

If no schedule project root resolves, for example because a volume is not
mounted, upgrade leaves the installed managed block unchanged and reports every
schedule as unavailable. It never clears managed cron entries that it cannot
re-render.

Use `--update-jobs` only when you want an available deterministic profile-job
format update and safe schedule refresh:

```bash
ricky upgrade --to X.Y.Z --yes --update-jobs
```

Representation-only and equal-or-narrower authority changes can refresh and
resync automatically. A provider, tool contract, mutation permission, source
scope, account, workflow argument, browser scope, effect budget, timing, or
approval-evidence expansion remains `approval_required`. The flag cannot grant
that authority. Review the schedule, then use the ordinary commands:

```bash
ricky schedule show SCHEDULE_ID
ricky schedule approve SCHEDULE_ID
ricky schedule sync
```

Authored job bundles are validation-only and remain byte-for-byte unchanged.
Update the owning bundle when a target release cannot validate one. Jobs
bundled with Ricky are read-only product surface; a release replaces them.

### Understand upgrade backups and retention

Before a migration write, Ricky integrity-checks declared SQLite stores and
creates a verified targeted backup at
`<user_data_dir>/upgrades/<operation_id>/backup/`. The manifest binds each
declared mutable path, file mode, size, and digest; SQLite capture includes WAL
state. It also verifies free space with a safety margin.

This is not a full copy of `user_data_dir`. Unrelated downloads, browser
profiles and binaries, attachments, and media are excluded unless a release
declares that exact format mutable. Resources bundled with Ricky ship in the
installed package, so a release replaces them and no backup covers them. Ricky retains
the active operation backup and the newest successful pre-upgrade backup,
removing older verified backup trees while preserving their operation
journals.

### Resume or roll back

After a failed or interrupted upgrade, do not edit the manifest, journal, or
backup. Ordinary stateful commands remain gated so old software cannot open
partly migrated data. Continue the exact recorded target with:

```bash
ricky upgrade --resume
```

Before the commit fence, restore the verified source data and exact source
wheel with:

```bash
ricky upgrade --rollback --yes
```

Rollback verifies the backup, restores only its declared paths and modes, and
reinstalls the exact prior release last. Both recovery operations are
resumable. A downgrade without this matching journal and backup is unsupported;
after the commit fence, move forward with resume or a later released upgrade.

## Decommission or remove an installation

Remove persistent launch surfaces before uninstalling the executable:

```bash
ricky decommission
```

The command stops, disables, and removes a Ricky-owned gateway user service,
removes Ricky's managed crontab block, and reports any crontab backup. It
refuses to modify a service unit that Ricky does not own. The teardown runs
even when the unit file is already gone, because systemd can still hold the
unit loaded, and a failing `systemctl` is reported as an exit code rather than
stopping the removal, so a host without a user session bus can still
decommission. It reads the user
crontab to decide what is installed, and reports no managed schedules when the
host has no usable crontab program, because an unusable crontab cannot hold the
block. Repeating the command is safe. It preserves `user_data_dir`, the
installation manifest, and the XDG bootstrap pointer.

Remove only the software with:

```bash
uv tool uninstall ricky
```

The preserved pointer lets a later reinstall rediscover the same data. If you
intend to destroy all Ricky data, do so before uninstalling the executable:

```bash
ricky decommission
ricky data purge
uv tool uninstall ricky
```

Interactive purge displays the canonical root and installation ID and defaults
to no. For unattended use, both confirmations are required:

```bash
ricky data purge --yes --installation-id EXACT_ID
```

Purge refuses an active gateway, an installation-ID mismatch, and an installed
Ricky-owned service unit or managed crontab block, so run `ricky decommission`
first. It removes the matching bootstrap pointer only after the exact
initialized data root has been removed. It is irreversible and never follows an
arbitrary path argument.

If deletion fails partway, Ricky does not restore the partly deleted root,
because the restored root would look healthy with data already missing. The
remains are kept beside the original path, the pointer is preserved, and the
error names the path to review and remove.
