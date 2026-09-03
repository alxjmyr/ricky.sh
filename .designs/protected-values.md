# Protected values

This document defines Ricky's agent-usable protected-value boundary. User-facing setup belongs in
`docs/`; exact schemas, limits, and cryptographic parameters belong in code and tests unless they
preserve a durable cross-component invariant.

## Purpose and threat boundary

`ricky.protected_values` lets trusted local capabilities use user-managed secret material through
opaque profile-qualified references without sending raw values to an LLM provider. It owns safe
catalog metadata, encrypted persistence, unlock lifecycle, destination policy, approvals, use
reservations, and non-secret evidence. Browser form filling is its first consumer, not its owner.

The primary guarantee is data-flow isolation: raw values never enter model requests, tool
arguments or results, ordinary context, session history, events, logs, traces, artifacts, media,
permission summaries, effect identities, or audit records. Authenticated encryption separately
protects copied durable storage while locked.

The boundary does not claim to protect values after an approved destination receives them, or
against a compromised local account, OS, browser, approved destination, or authorized Ricky
process after unlock. Python cannot guarantee process-memory zeroization. Implementations minimize
the lifetime and reachability of materialized values without claiming stronger erasure.

## Ownership and dependencies

`ricky.protected_values` owns identities, kinds, fields, safe descriptors, revisions, policies,
the backend protocol, unlock slots, injected secure responders, scope and destination enforcement,
durable approvals, use reservations, and safe catalog tools. Consumers import the broker contract.
The broker never imports browser, interface, provider, or Playwright packages. Interfaces implement
responder protocols and inject them at composition roots. Provider adapters never receive a broker
or materialized value. The broker is not added to general `ToolContext`; only trusted purpose-built
consumer tools receive it directly.

Infrastructure credentials remain typed `RickySettings` values loaded by `ricky.config` from the
owning profile's `.secrets.toml` or environment. They are not imported into or listed by the
protected-value catalog.

## Identity, scope, and metadata

Protected resources use canonical `ProfileResourceRef` identity. A broker receives an immutable
`ProfileScope` before opening stores or listing resources. It may access only owning profiles in
that scope, requires qualified references, and cannot widen child work.

Safe descriptors contain only user-authored aliases, labels, descriptions, value kinds, field
labels and materialization modes, policy summaries, revisions, enabled state, and owner. Ricky
does not derive usernames, card digits, lengths, hashes, or hints from raw values. Safe metadata
may reach the configured model and documentation warns users to name resources accordingly.

Field materialization mode is versioned per field: `stored` or `prompt_each_use`. It is policy,
not a permanent restriction of most field types. One-time-code fields remain prompt-each-use.
Card security codes default to prompt-each-use but may be explicitly authored as stored for the
reviewed Phase 7 gateway path; destination checks, unattended policy, reservations, and commit
ceilings still apply independently.

## Storage and unlock

Each profile owns one SQLite store below its profile data root. The store contains safe metadata,
policies, approvals, reservations, audit records, ciphertext payloads, and wrapped keys. It never
lives in project `.ricky/`. Directories and files are owner-only on POSIX, schemas are versioned,
and unknown versions fail without rewrite.

Every vault has one random data-encryption key. Protected payloads use authenticated encryption
and bind profile, resource identity, field schema, and revision inside the encrypted envelope. An
interactive passphrase is processed with Argon2id to derive a key-encryption key that wraps the
data key in one versioned unlock slot. Each slot persists its salt and exact KDF algorithm
parameters, so later installation-setting changes cannot strand an existing vault. Passphrase
rotation rewraps the data key with the then-configured parameters without re-encrypting protected
resources.

Unlock method is independent of payload encryption. The current durable mechanism is one hidden
interactive passphrase slot. The slot collection and `UnlockProvider` boundary permit a later OS
keyring, TPM or system credential, hardware key, or external-vault slot without migrating resources
or changing consumers. Ricky never stores the passphrase beside the vault by default.

`ricky.protected_values` also owns a process-local resident unlock registry. An interface may prompt
at gateway process startup for explicitly named profiles and supply those passphrases directly to
the registry. The registry unlocks each profile backend, drops the passphrase, retains only the
unwrapped backend/data key for that process lifetime, and issues scope-narrowed broker leases to
trusted consumers. It imports no gateway, browser, interface, provider, or Playwright package.

Direct `gateway run` prompts in the gateway process. Managed service start/restart uses an
owner-only, single-consumption POSIX Unix-socket bootstrap rendezvous: the invoking CLI prompts,
the new gateway proves same-user and current lock ownership, each bounded passphrase frame is
acknowledged, and both sides close and unlink the rendezvous on success, failure, cancellation, or
timeout. Passphrases never enter arguments, environment settings, service-unit text, regular
temporary files, configuration, SQLite, logs, messages, or output. Requested unlock is all-or-none;
failure exits the new gateway. Startup or restart without the option, automatic restart, crash, or
process replacement is locked.

Synchronous storage work runs in joined worker threads. Cancellation waits for an in-flight
transaction or publication to finish before propagating so state is observably old or new, never
orphaned. The broker drops its unwrapped data key on close or lock.

## Destination and use policy

Materialization uses actual current destination facts from a registered local consumer, never a
model-supplied origin. Policy supports exact authored origins, fresh confirmation of a new origin,
previously approved exact origins, and explicitly broad public HTTPS use. Origins are exact
scheme/host/port values; Ricky does not infer registrable domains, wildcard subdomains, or
organizational trust.

`confirm_new` uses an injected trusted responder that can deny, allow once, or durably approve the
exact top-level and target-frame origin pair. Only the explicit durable choice mutates approval
state. Approvals remain local, inspectable, and revocable and are not ordinary session permission
grants. A gateway-owned execution can park one exact protected-fill occurrence for authenticated
one-execution destination authorization; this does not mutate durable destination approval. Other
non-interactive surfaces deny new-destination confirmation.

`secure_web` means public HTTPS only. The broker resolves hostnames and rejects the destination if
any resolved address is private or special, independently of ordinary browser exceptions, both at
materialization and dispatch revalidation. A private or loopback HTTPS destination instead
requires an exact non-`secure_web` protected policy and ordinary consumer destination policy.
Production plain-HTTP materialization is denied. Missing, opaque, credential-bearing, changed, or
policy-revoked destinations fail before materialization or dispatch.

Every resource carries independent foreground-use, unattended-use, and unattended-commit policy.
Unattended fields default closed and include per-execution materialization and commit counts plus
an optional exact-currency amount ceiling. Gateway-owned ad hoc executions may materialize only
through a currently resident unlocked profile and an exact compiled broker lease. Named jobs,
scheduled jobs, workflows, and a gateway started locked expose no unattended materializing tool.

## Materialization and consumers

The broker releases raw values only to a registered in-process consumer after resource scope,
state, revision, field compatibility, destination policy, approval, execution mode, and ceilings
pass and a use is reserved. Raw payloads use an in-process-only `SecretStr` container with no useful
string or repr. The reservation is finalized conservatively when material is released, cancelled,
or fails.

There is no generic reveal, clipboard, shell, arbitrary HTTP, header, template, or plugin secret
tool. The model-facing catalog is read-only safe metadata. Each consumer adds a purpose-built tool
with its own destination, effect, permission, and evidence contract and receives the broker only by
explicit runtime composition.

The initial browser consumer fills one snapshot-bound field per call. It locally derives the live
top-level and target-frame origins and protected field category, then prepares exact secret
material after protected policy and secure input. Standard permission review contains only the
alias and safe target facts. Dispatch revalidates page generation, target, origins, policy,
approval, and resource revision and sends the raw value only through a dedicated in-process backend
request. Ordinary fill, key entry, and coordinate actions continue to reject protected controls.

For an unattended `confirm_new` destination, the generic broker request carries an optional
consumer-owned safe occurrence binding: generation, observation identity, and target identity.
The gateway browser consumer requires it, parks one source-bound approval, and consumes it once.
The binding contains no browser handle or protected material and does not create a durable
destination approval. A consumer that cannot provide an exact binding cannot use this approval
path.

Protected fill is an external effect because page JavaScript may observe or autosave a field. It
is never retried after dispatch may begin. Filling never submits, clicks, presses Enter, or grants
commit authority. Browser commit remains a separate destructive review.

A later financial browser approval may name a successfully used protected resource only by its safe
profile-qualified alias. The browser consumer can retain that non-secret current-page evidence, but
the broker does not construct, approve, or dispatch the transaction envelope. A website-displayed
masked funding-source label is untrusted page content and is never derived from broker material.

After one exact background transaction approval is consumed but before browser dispatch, the
consumer asks the broker to reserve every protected source participating in the commit. The broker
checks execution identity, exact resource revision and fields, unattended-commit enablement,
per-execution commit count, amount, and currency against the resource policy. The reservation binds
the stable logical effect key and envelope digest. Deterministic no-dispatch finalizes it as
`not_performed`; performed or ambiguous dispatch consumes it as `performed` or `in_doubt`. Raw
values never enter this commit record. This is a reusable broker API; the browser remains only one
consumer.

## Change checklist

1. A profile scope exists before catalog or store access, and qualified identity cannot widen it.
2. Raw values never enter provider, tool, event, session, log, trace, artifact, or evidence models.
3. Storage encryption, unlock source, destination policy, and consumer effect authority remain
   separate contracts.
4. No generic agent operation can reveal or route a value to an arbitrary sink.
5. Every materialization is exact-revision, destination-bound, reserved, and conservatively
   finalized.
6. A consumer revalidates mutable destination and target facts immediately before dispatch.
7. Materialization cannot imply transaction commit or unattended authority.
8. Cancellation joins storage and consumer work and never permits unsafe replay.
9. Resident unlock is explicitly profile-scoped, process-lifetime-only, and independent of use or
   transaction authority.
10. Unattended protected commit policy is reserved after exact transaction approval and before the
    consumer dispatch, then finalized conservatively from effect evidence.
