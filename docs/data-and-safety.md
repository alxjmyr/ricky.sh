# Understand Ricky's data and safety model

Read this page before you connect external services or enable unattended work.

## Keep secrets profile-owned

Store credentials in `<user_data_dir>/profiles/<name>/.secrets.toml`. Use
the `shared` profile only for a credential intentionally available in every context. Ricky uses
redacted secret types and reports only whether a credential is configured.

Do not commit profile secret files, OAuth tokens, messaging bot tokens, provider API keys, runtime
databases, or transcripts.

A newly initialized installation enables only `shared`. A credential saved
there is available to every future profile as well as the current shared-only
runtime. Ricky does not silently move shared credentials or data when a
narrower profile is added.

Agent-usable protected values use a separate encrypted profile vault and qualified safe aliases.
Raw values remain outside model requests and ordinary evidence, but aliases, labels, field names,
and destination policy are provider-safe metadata. Do not put secret material in those fields.
The vault passphrase is entered locally and is not an infrastructure setting. See
[Protected values](protected-values.md) for its at-rest and local-host threat boundary.

A gateway starts with protected-value vaults locked unless its local operator names profiles with
the repeatable `--unlock-vault PROFILE` startup option. Direct gateway runs take the singleton lock
before prompting. Managed service startup uses an owner-only, one-use local socket and verifies
that the receiver is the same-user process holding the current gateway lock. Passphrases do not
enter systemd arguments, environment variables, unit text, configuration, logs, or regular files.
They are discarded after unwrapping the resident vault key.

Gateway unlock is process-local and is not durable authority. Exit, crash, replacement, an
automatic service restart, or an explicit restart without the option returns to the locked state.
Unlocking a vault does not widen profile scope, approve a destination, or grant a consumer tool.
The unlocked key is available to the authorized Ricky process, so this mechanism does not protect
against a compromised local account or that process while it remains unlocked.

Background browser evidence stays in the existing execution and job stores. Ricky persists bounded
attempt, budget, safe origin, transaction-envelope, approval, effect-disposition, cleanup, and
operator-reconciliation records. It does not persist screenshots, DOM dumps, cookies, request or
response bodies, decrypted protected values, prepared effects, browser-native handles, or live
sessions for restart. A parked transaction therefore fails closed if its owning process or browser
is lost.

## Know what a profile scope permits

Every runtime receives an immutable scope before Ricky selects a provider, assembles context, or
constructs tools. The scope always contains `shared`, has one primary profile, and may contain
additional readable profiles. A child agent can retain or narrow that scope but cannot add access.

The primary profile supplies ordinary defaults, persona, and the default write destination.
When `shared` is primary, its settings and persona are applied exactly once.
Additional profiles supply resources without adding their persona. Profile-owned memory, skills,
workflows, jobs, accounts, credentials, tasks, downloads, and temporary data are discoverable only
inside the issued scope. Derived records carry a profile label, and their stores enforce it on
reads, lists, mutations, recovery, retention, and audit.

Provider selection is separate. A provider must be allowed by every profile in scope; choosing a
provider never grants access to another profile.

## Protect installation identity and private files

Ricky stores its host-local bootstrap pointer at
`${XDG_CONFIG_HOME:-~/.config}/ricky/bootstrap.toml`. The pointer and
`<user_data_dir>/installation.json` carry the same opaque installation ID.
Ricky fails closed if they disagree.

Ricky-created private directories use owner-only permissions (`0700`) and
private files use `0600` on supported POSIX systems. Initialization refuses a
symlinked root, a root owned by another user, the filesystem root, the user's
home directory itself, and unknown nonempty directories. It stages and
atomically replaces complete files and scaffolds.

`ricky decommission` preserves both data and the pointer. `uv tool uninstall
ricky` removes only software. The separate `ricky data purge` command is
irreversible: it displays the exact canonical root and installation ID,
requires confirmation, refuses an active gateway or any Ricky-owned launch
surface that would outlive the data, and removes only the data bound to the
matching pointer and manifest. If deletion fails partway it never restores the
partly deleted root; the remains are kept beside the original path for review.

Released upgrades keep software ownership separate from data ownership. `uv`
owns the tool environment and executable; Ricky only coordinates an exact,
digest-verified `uv tool` replacement. Upgrade artifacts, journals, and
targeted backups live below `user_data_dir/upgrades`, but installed application
code does not.

Upgrade takes an installation-wide exclusive lock and gates ordinary stateful
commands whenever the manifest is not clean. Its strict journal binds the
installation identity, source and target releases, data generations, migration
plan, backup, and managed-launch choices. Resume and rollback follow that
record; they do not rebuild intent from changing live configuration.

The pre-upgrade backup covers only subsystem-declared mutable paths. It is not
a general backup of browser assets, downloads, attachments, media, or project
content. SQLite stores are integrity-checked and captured with WAL state;
declared files retain modes and digests. Rollback verifies this evidence before
restoring it and cannot serve as an arbitrary downgrade mechanism.

Only deterministic profile-owned job migrations may write authored job files,
and only when requested with `--update-jobs`. Resources bundled with Ricky are
read-only; a release replaces them. Safe representation or authority narrowing can preserve a
schedule, but any authority expansion still requires the ordinary explicit
schedule approval. Until then, desired schedule state is retained while its
managed cron entry is withheld.

Notification correlations always carry a profile label. Records created by an earlier development
version without correlation labels are not compatible with this format. Migrate those records or,
for disposable development data, recreate the notification store before upgrading.

## Know what reaches a model

Model requests can include your prompt, prior chat history, active skill instructions, memory
indexes, workflow inputs, and tool results from the issued scope. Use `/context` to inspect
categories and estimated size. `/debug` can show non-secret user content in your terminal or logs.

Browser snapshots can include personal data rendered by signed-in pages. Persistent Chromium
profiles retain cookies, local storage, cache, and account sessions below their owning Ricky
profile. Ricky does not export those stores or add application-level encryption at rest. Opening a
configured browser resource requires fresh local permission because bounded page observations may
be sent to the configured model provider.

Browser screenshots are untrusted model input and can contain ambient personal data even after
Ricky masks editable, credential, payment, OTP, and file controls. They are denied unless the
resource owner's profile explicitly allows the session provider. This is a standing disclosure
allowance: read-only captures do not ask again individually, and the allowance applies to the
provider rather than one exact model. Screenshot history stores only opaque image references; a
provider-bound resolver rechecks profile scope, disclosure policy, dimensions, size, and digest
immediately before wire encoding. Session screenshot files are removed when the resident runtime
closes or `/clear` starts a fresh session. Coordinate clicks remain separately reviewed effects.

Recognized protected browser controls expose only a safe category. The dedicated protected-fill
tool receives an alias and field name, derives current origins locally, and sends the raw value
only to the exact browser control after policy and permission review. Filling never includes
transaction commit authority.

Every consequential browser commit requires a fresh financial or generic browser transaction
approval. Financial reviews identify the proposed total, currency, payee, fees, recurrence, and
safe funding-source alias or label. Generic reviews identify the intended action, destination,
consequences, and disclosures. Proposed business details remain model-authored; locally verified
browser binding is displayed separately. An allow rule cannot suppress this fresh review, and a
completed click does not prove remote settlement or acceptance. See
[Browser transaction approvals](browser-transactions.md).

## Review permissions

| Tool risk | Default behavior |
|---|---|
| Read-only | Allow |
| Mutating | Ask |
| Destructive | Ask |

A denial policy takes priority over a session grant. Profile routing does not add a redundant
approval; the normal permission and effect checks still apply to the selected operation.

External actions such as sending a message or email show an action-specific preview. If an
interrupted call has an ambiguous result, Ricky records it as uncertain or in doubt instead of
guessing whether it happened.

## Understand host-file and export boundaries

File tools accept project-relative, home-relative, and absolute paths. Access outside the active
project requires permission for the resolved canonical path. Symlinks are authorized against their
destination.

Outbound attachments can use ordinary host files, but generic paths cannot export Ricky-owned
configuration or runtime state. Durable-task artifacts use logical task ID, artifact path, and
owning profile; the profile must be in the session scope. Integration downloads and generated
artifacts live under their owning profile root, not project `.ricky/`.

Prepared browser uploads freeze exact bytes before permission review and never reopen the source
path for dispatch. Browser downloads publish only from Ricky-owned browser sessions, use plain
filenames without replacement, and return logical digest-bound references. Durable browser
downloads remain below the owning profile after the browser session closes.

## Treat unattended work as a separate trust decision

Jobs, schedules, the gateway, and execution workers can run without an interactive prompt. They
use explicit capabilities, fixed budgets, immutable requests, profile scopes, and durable evidence.

Before unattended use:

1. Inspect and validate the job, workflow, route, or capability.
2. Use a dry run when available.
3. Check the complete profile scope and selected provider.
4. Review exact effect and notification routes.
5. Keep budgets and allowed capabilities narrow.

Standing capability policy controls eligibility, not approval for every possible action.
