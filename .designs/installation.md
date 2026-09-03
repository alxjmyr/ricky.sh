# Installation and local lifecycle

This document defines Ricky's released-software installation boundary, local
bootstrap identity, first-run scaffold, and removal lifecycle. User-facing
commands belong in `docs/getting-started.md` and `docs/cli-reference.md`;
canonical models and state transitions live in `ricky.installation`.

## Ownership boundary

Released Ricky is installed as a versioned wheel from GitHub Releases with
`uv tool`. The external installer owns the executable, Python environment, and
software removal. Ricky never installs application code below `user_data_dir`.
During an upgrade Ricky coordinates `uv tool` to replace that external
environment; it does not write application code itself.

Ricky owns only:

- one host-local bootstrap pointer;
- one initialized `user_data_dir` and its private contents; and
- managed runtime integrations, such as Ricky-owned service and schedule
  definitions, while the software remains installed.

Each release publishes a strict descriptor, an exact wheel with its SHA-256,
and an immutable dependency-constraints artifact with its SHA-256. The
constraints artifact pins the complete production dependency set used to
create or restore that release's tool environment. The descriptor also binds
the release version, supported source and target data generations, Python
requirement, minimum uv version, repository identity, and applicable artifact
names. Upgrade and rollback give uv both the exact wheel and its verified
constraints; a wheel identity alone is not a reproducible software backup.

The released surface supports Linux/POSIX. Optional features retain
their own capability requirements: systemd commands require a user systemd
manager, schedule commands require compatible cron support, and browser
control requires the host libraries supported by the pinned Playwright build.
Ricky diagnoses missing host capabilities; installation does not silently
modify system packages.

## Bootstrap identity

The fixed machine-local pointer is:

```text
${XDG_CONFIG_HOME:-~/.config}/ricky/bootstrap.toml
```

The pointer is a strict, versioned, non-secret document containing the
canonical absolute `user_data_dir` and an opaque installation ID. The same ID
appears in `<user_data_dir>/installation.json`. A mismatch fails closed so a
copied or redirected pointer cannot silently select unrelated data.

`XDG_CONFIG_HOME` selects the host configuration root. It is not a Ricky
setting. `RICKY_USER_DATA_DIR` remains the only Ricky-specific environment
variable that can participate in bootstrap selection.

Before a pointer exists, initialization chooses a target in this order:

1. The explicit `ricky init --user-data-dir PATH` value.
2. `RICKY_USER_DATA_DIR`.
3. `~/.ricky`.

After a pointer exists, it is authoritative. An explicit or environment value
may confirm that exact canonical root but cannot select a different one. Ricky
does not move or repoint an initialized data root. Removing all installation
data through the guarded purge lifecycle also removes the matching pointer and
allows a future fresh initialization.

The pointer is the sole persisted authority for `user_data_dir`. Generated
`ricky.toml` does not repeat the root, and file loading cannot redirect the
bootstrap selection.

## Installation manifest

`<user_data_dir>/installation.json` is a strict JSON-round-trip-safe model. Its
format records:

- installation format version;
- installation ID;
- Ricky version that created the installation;
- Ricky version of the last lifecycle operation that wrote the manifest;
- UTC creation time; and
- the monotonic whole-installation data generation;
- structured migration state; and
- the opaque upgrade operation ID while an operation is in progress.

A fresh installation starts at data generation `1`, migration state `clean`,
and a null operation ID. Defaults for those fields keep an existing format-1
manifest valid without rewriting it. Defined migration states include
`clean`, `prepared`, `software_replaced`, `migrating`, `failed`, and
`rolling_back`. A clean manifest has no operation ID; every non-clean upgrade
state names the journal that controls recovery. Manifest transitions are
strict, atomically durable, and made only while holding the exclusive
installation operation lock.

`last_lifecycle_version` is written only by an operation that holds the
exclusive installation lock and rewrites the manifest. Creation and upgrade
write it. Verifying an existing installation does not rewrite it, so it records
the last lifecycle operation rather than the last version that ran. It is an
audit and release identity, never a migration gate.

Software version, bootstrap format, manifest format, data generation, and each
subsystem schema version are separate axes. Data generation is the runtime
compatibility gate. Several software releases may share one generation, and a
release may provide a complete declared path from more than one supported
source generation. Older software never opens a newer generation. A downgrade
is supported only by restoring its matching pre-upgrade backup before
reinstalling its exact wheel and constraints.

## Initialization

`ricky init` is offline, noninteractive, deterministic, idempotent, and safe to
script. It creates only the minimal scaffold:

```text
<user_data_dir>/
├── installation.json
├── ricky.toml
└── profiles/
    └── shared/
```

The generated registry enables only `shared` and makes it the default. No
secret file, database, browser binary, service, schedule, log, or optional
feature state is created by initialization.

Target handling is exact:

| Target state | Behavior |
|---|---|
| Missing | Create and initialize it. |
| Existing and empty | Initialize it. |
| Current valid installation | Verify identity and report success without rewriting it. |
| Valid installation with no pointer | Bind it only through explicit initialization of that same root. |
| Pointer with a missing or empty data root | Recreate the scaffold under the pointer's recorded installation ID. |
| Recognizable but partial installation | Refuse and identify the invalid or missing scaffold. |
| Unknown nonempty directory | Refuse without writing. |
| Invalid or newer manifest/pointer format | Refuse without attempting repair. |
| Pointer and manifest ID mismatch | Refuse without changing either location. |

Initialization stages the complete scaffold beside the target and renames it
into place. The pointer is written last. If pointer creation fails, the valid
orphaned scaffold can be bound by running `ricky init --user-data-dir` with its
exact root. Two simultaneous lifecycle operations are excluded by the
host-local installation operation lock.

Target selection and validation are separate from writing. A refused target is
rejected before the lock is taken, so an initialization that never starts
creates neither the bootstrap directory nor its lock. The same selection is
revalidated under the lock, and that outcome is the authoritative one.

## Filesystem guarantees

On supported POSIX systems, Ricky-created private directories use mode `0700`
and private files use `0600`. Writes use same-directory temporary files,
`fsync`, and atomic replacement. Initialization:

- resolves and displays canonical absolute paths;
- refuses the filesystem root and the user's home directory as a data root;
- refuses symlinked roots, including dangling links, non-directory roots,
  and roots owned by another user;
- does not adopt unknown files; and
- never creates a secret file until a credential is actually saved.

Existing valid installations are verified rather than recursively rewritten.
Subsystems continue to own permission checks for state they create later.

## Guided setup and optional components

`ricky setup` is the separate interactive, resumable path from an initialized
scaffold to a minimal working shared-profile chat configuration. It uses
hidden input for an API credential, verifies local Claude Code availability,
and saves the selected provider and model through the typed configuration
boundary. Initial setup performs local checks only and makes no provider
request.

Every network action is identified before it runs:

- provider verification is authenticated and may be billable;
- OAuth authorization contacts an external service and may open a browser; and
- browser installation downloads the Chromium build matched to Playwright.

Declining remote verification does not invalidate locally valid configuration.
Optional assets, integrations, services, and unattended work remain separate,
explicit actions. Feature code may ship in the wheel without making the
feature configured, enabled, downloaded, or running.

When `shared` is the only profile, configuration, credentials, and user data
stored there retain their intentionally universal meaning if more profiles are
added later. Setup must make that consequence visible and must never silently
move shared-owned data.

## Operation lock and compatibility gate

The host-local operation lock lives in the bootstrap directory, outside the
data root, so purge cannot remove the coordination object. It has shared and
exclusive forms and every acquisition has a bounded wait with a sanitized
active-operation diagnostic.

- An ordinary stateful Ricky process holds a shared lock for its complete
  lifetime. The gateway holds it for its complete service lifetime. A
  cron-launched process must acquire it before opening durable state.
- Initialization, decommission, purge, upgrade, resume, and rollback hold the
  exclusive form for every mutation and verification in their operation.
- Every stateful entry point checks pointer/manifest identity, requires a clean
  migration state, and verifies that its software supports the manifest's data
  generation before it loads configuration or opens subsystem state.

`--help`, `--version`, upgrade recovery, `ricky decommission`, and guarded
`ricky data purge` remain available when the compatibility result is not clean.
Those paths may read only the bootstrap, manifest, operation metadata, and the
launch surfaces needed for recovery or safe removal; they do not open ordinary
durable subsystems. Decommission and purge accept every defined migration state
but continue to require exact installation identity and their existing
liveness protections.

A running gateway cannot be stopped after the upgrader takes the exclusive
lock because the gateway owns a shared lifetime lock. Upgrade therefore first
verifies the installed unit and exact launcher binding, records whether the
owned unit is installed, enabled, and active, requests stop while no exclusive
lock is held, and waits boundedly for the gateway to drain and release its
shared lock. It then acquires the exclusive lock and rechecks identity, unit
ownership, launcher binding, gateway inactivity, versions, and release input.
A stop failure, drain timeout, restarted gateway, foreign unit, or changed
launcher aborts before upgrade state is prepared. The unit is restarted only
if it was active beforehand and the completed upgrade and post-upgrade health
check both succeed. If an unchanged installation aborts before `prepared`, the
upgrader releases its operation lock and attempts to restore that prior running
state with the verified unchanged launcher; a failed restoration is reported
rather than hidden.

Managed systemd and cron launch surfaces own an exact absolute `ricky` console
script from the installation's uv tool environment, not a PATH lookup, an
arbitrary `uv`, or a source checkout. A marker permits Ricky to consider a
file, but upgrade ownership additionally requires its parsed launch command to
bind the expected installation and exact installed launcher. A foreign or
ambiguous surface is never stopped, rewritten, or removed. Reconciliation
rewrites owned surfaces to the verified target launcher before they can run
again.

## Upgrade and recovery

The released lifecycle surface is:

```text
ricky upgrade --check [--to VERSION] [--json]
ricky upgrade [--to VERSION] [--yes] [--update-jobs] [--json]
ricky upgrade --resume [--json]
ricky upgrade --rollback [--yes] [--json]
```

`ricky upgrade --check` is read-only apart from bounded release lookup and
cache behavior. By default it queries stable GitHub Releases over anonymous
HTTPS and stores no release credential. Apply is available only from a conforming
`MAJOR.MINOR.PATCH` release running through its exact console script inside the
matching uv tool environment. A development checkout may check but may not
replace an installed tool. Interactive apply selects a stable release and
shows the exact target, data root, backup estimate, service interruption, and
scheduled-job actions before confirmation. Unattended apply requires `--yes`
and exact `--to VERSION` together. Prereleases, downgrades, ambiguous assets,
unapproved redirects, incompatible generations, missing or old uv, and
nonconforming versions fail before mutation. JSON mode emits exactly one strict
result or error document with bounded diagnostics and no secrets, credentials,
or protected values.

The upgrade coordinator plans all changes before writing. It discovers only
declared configured paths, includes disabled profiles, preserves missing lazy
state as missing, groups adapters that share a physical path, orders their
steps, and records a digest of the complete plan. The manifest carries only the
compatibility gate. Detailed progress lives separately at
`<user_data_dir>/upgrades/<operation_id>/journal.json`, including source and
target software and generations, step IDs and plan digest, last verified step,
backup identity and digest, timestamps, and a bounded sanitized failure. A
corrupt journal cannot make the manifest unreadable or prevent decommission or
guarded purge. Once the manifest enters `prepared`, that exact journal is the
authority for resume or rollback. Recovery is directional: after an operation
enters `rolling_back`, resume refuses to move it forward again and directs the
operator to complete the rollback, because data may already be partly restored
to the source. Backup retention is housekeeping. Past the commit fence, with the
target installed, migrated, and verified, no retention or prior-operation error
may drive the installation to `failed`. Whole-installation verification is required
before the target generation can return to `clean` and clear its operation ID.

The old process may coordinate uv replacement, but the target executable owns
the migration. The parent passes the already-locked operation file descriptor
to exactly that child as an explicitly inherited descriptor, together with the
exact operation ID and target identity. The child verifies the descriptor's
lock-file identity, its own installed launcher and version, the journal
binding, pointer and installation IDs, and target before accepting the
handoff. Parent and child close their duplicate descriptors only in an order
that leaves at least one holder for the full exclusive operation. The child
does not close and reacquire the lock, so executable replacement creates no
window in which another Ricky process can open state.

Each durable subsystem owns its own migration adapter and remains the sole
owner of its SQL and serialized-state formats. An adapter exposes stable step
identity, supported schema transitions, non-creating discovery and inspection,
structural and integrity validation, preflight size, exact backup targets,
idempotent transactional apply, and post-migration verification. It also
declares whether it touches authored files, encrypted bytes, or managed
integrations. The coordinator resolves paths, groups physical stores, orders
and hashes the plan, and journals progress; it never embeds another
subsystem's SQL or becomes a second schema owner. After adapters own legacy
transitions, an ordinary store open may create a genuinely absent current
store or validate an existing one, but it never migrates existing state as a
side effect.

## Backup and rollback boundaries

Before the first migration write, upgrade checks every declared SQLite target,
captures it with the SQLite backup API including WAL state, and copies every
declared mutable non-SQLite file. The strict backup manifest binds each
absolute source identity to a confined backup-relative path, size, private
mode, and SHA-256. Upgrade checks free space with a safety margin and fsyncs
the verified backup before advancing the journal. Backups cover only targets
declared mutable by the plan; unrelated immutable downloads, browser assets,
attachments, and media are excluded unless a specific migration declares
otherwise. Resources bundled with Ricky live in the installed package rather
than `user_data_dir`, so a release replaces them and no backup covers them.

The backup lives at
`<user_data_dir>/upgrades/<operation_id>/backup/`. Ricky retains at most the
operation in progress and the newest successful pre-upgrade backup. Purge
removes them with the data root. Rollback is a forward recovery operation, not
a reverse migration: under the exclusive lock it verifies the backup manifest,
restores the exact declared files and modes, verifies their schema versions and
source data generation, and finally reinstalls the prior exact wheel with its
verified dependency constraints. It is resumable and refuses to overwrite
evidence that a successfully opened post-upgrade runtime could have mutated.
Resume continues only the exact journaled target and plan. An applied step is
replayed only when inspection proves its idempotent completion state.

Upgrade never calls a model provider, performs an external business effect,
unwraps protected values, or changes the installation or profile roots.
Protected-value migration preserves ciphertext without a vault passphrase.
Authored skills and workflows are validate-only, and resources bundled with
Ricky are read-only. Unknown or corrupt formats fail before mutation, and ordinary
stateful commands remain gated after a failed replacement until resume or
rollback restores a verified clean software/data pair.

## Scheduled jobs during upgrade

Only profile-owned job bundles may be migrated, and only by a deterministic
strict source-to-target adapter when job updates were requested. Project-owned
jobs are validated under the target runtime but never rewritten. A safe
representation-only or authority-equal/narrower change preserves the schedule
ID, cron expression, enabled state, profile scope, project root, and prior
approval evidence, then uses the ordinary schedule refresh policy and resyncs
the exact-launcher managed cron block. `--update-jobs` grants permission to
migrate representation and reconcile schedules; it never grants or widens
unattended authority.

Any provider, tool contract, mutation permission, source scope, account,
workflow argument, browser or disclosure scope, effect budget, cron expression,
or approval-evidence expansion uses the ordinary explicit schedule approval
path. An unattended upgrade cannot supply that approval. Invalid or unavailable
jobs keep desired schedule state but are omitted from installed cron until
operator action makes them valid. Software/data success and schedule readiness
are reported separately.

## Removal lifecycle

Removal has three deliberately separate boundaries:

1. `ricky decommission` removes Ricky-managed persistent launch surfaces, such
   as its owned gateway user service and managed crontab block. It refuses an
   unowned service unit, is idempotent, and preserves the bootstrap pointer and
   all user data. Removing the owned unit file is what retires the service, so
   the teardown always runs and reports each service-manager exit code instead
   of aborting before removal. A host with no reachable user service manager,
   or a unit the manager still holds loaded after its file was removed out of
   band, must not make decommissioning impossible. A launcher that survives a
   failed stop is still caught by the active-gateway guard in purge.
2. `ricky data purge` irreversibly removes one exact inactive
   `user_data_dir`. It verifies the pointer and manifest installation IDs,
   displays the canonical root and ID, requires explicit confirmation, and
   removes the matching pointer only after data removal succeeds.
3. `uv tool uninstall ricky` removes software. It does not remove or offer to
   remove Ricky data.

Users decommission before removing software because Ricky cannot clean up
managed launch surfaces after its executable is gone. Purge is optional and
is never implied by either decommissioning or software uninstall. Data purge
must fail when identity cannot be proven; it never accepts an arbitrary path as
a recursive deletion target.

The purge API, not one command, enforces every liveness invariant. Under the
same lifecycle lock and before anything is staged or removed, it refuses an
active gateway, an installed Ricky-owned service unit, and an installed managed
crontab block. A launch surface that survives its data root is the failure being
prevented: a supervised service restarts against a root that no longer exists,
and a managed schedule keeps firing with no installation left to remove it. A
command may repeat a refusal earlier for a better message, but no caller can
reach deletion through an unguarded path. The host crontab program is an
injectable dependency of that API, so callers and tests supply their own
backend.

Installed managed schedules are determined by reading the user crontab, never by
the presence of Ricky's cron state directory, which can be deleted or restored
independently of the crontab. An unreadable crontab means no managed block: a
crontab that cannot be read could not have installed one. A successful read that
contains the block, or an ambiguous marker state, refuses. Decommissioning uses
the same signal, so an unavailable optional platform command is not a
decommissioning failure. Purge stages the root beside itself and then
removes it. Staging is the last reversible step: a purge that fails after
removal starts never restores the partially removed tree, because a restored
tree can satisfy the scaffold check while user data is already gone. The
remains stay at the staging path, the pointer is preserved, and the failure
names the path for manual review. Unattended purge requires both `--yes` and
the exact `--installation-id`. Lifecycle commands that support `--json` emit one
machine-readable result and machine-readable errors, so `--json` purge requires
the same two arguments instead of prompting.

Data-root relocation and multiple simultaneous local Ricky installations are
outside this contract.
