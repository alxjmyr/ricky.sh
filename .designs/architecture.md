# Ricky architecture

This document is the current architectural contract for Ricky. It describes
boundaries future changes must preserve; it is not an implementation history or
roadmap. Git retains superseded designs and their chronology.

User-facing behavior belongs in `docs/`. Exact schemas, event variants, limits,
and state transitions belong in code and tests unless they define a durable
cross-component boundary.

## Principles

1. **Deterministic harness, stochastic model.** Models propose reasoning and
   actions. Code owns context construction, capability exposure, validation,
   permission, execution, persistence, retry, and recovery.
2. **Provider-neutral core.** Ricky owns canonical Pydantic messages, requests,
   tool calls, results, and events. Provider wire formats remain inside their
   adapters.
3. **Explicit authority.** Discovering or naming a capability never grants the
   right to use it. Interactive permissions and unattended contracts are
   enforced before dispatch.
4. **Observable state machines.** Turns, workflows, jobs, queues, effects, and
   recovery emit or persist typed evidence. Ambiguous work is represented as
   ambiguous rather than inferred successful or safe to retry.
5. **Async ownership.** Core APIs are async. A component closes the resources
   and cancels and awaits the tasks or subprocesses it creates. Sync entry
   points exist only at interface edges.
6. **Serializable boundaries.** Cross-component state uses strict Pydantic
   models that survive a JSON round trip. Secrets use `SecretStr` and never
   enter prompts, events, logs, or user-visible output.

## Component ownership

| Area | Responsibility |
|---|---|
| `interfaces/` | Typer/Rich CLI entry points and concrete messaging transports; owns wire rendering and sync-to-async entry. |
| `runtime/` | Builds one owned capability and session runtime so the advertised and callable resource snapshots match. |
| `agent/` | Agent turn state machine, ordinary context assembly, serializable session state, compaction, typed events, and workflow execution service. |
| `llm/` | Canonical LLM types, provider protocol and factory, and the OpenRouter, Anthropic, and Claude Code adapters. |
| `tools/`, `skills/`, `capabilities/`, `permissions/`, `authority/` | Executable tools, passive prompt resources, installed capability inventory, interactive policy, and specialized delegated constraints. |
| `workflows/`, `jobs/`, `schedules/`, `executions/` | Typed execution DAGs, bounded unattended runs, cron desired state, and immutable ad hoc execution contracts and requests. |
| `profiles/`, `memory/`, `durable_tasks/`, `sessions/` | Runtime access scope, durable knowledge, durable responsibilities, and fenced persistent conversations. |
| `media/` | Provider-neutral session media admission, confined storage, retention, and provider-bound materialization. |
| `protected_values/` | Profile-scoped encrypted agent-usable values, safe catalog metadata, unlock, destination policy, approvals, and local materialization. |
| `browser/` | Profile-scoped browser resources, lifecycle, semantic observations, actions, attachment, and browser-specific safety policy. |
| `notifications/`, `messaging/`, `gateway/` | Logical-route outbox, platform-neutral inbox/delivery, persistent conversation coordination, recovery, health, audit, and retention. |
| `installation.py` | Host-local bootstrap identity, operation locking, compatibility gating, atomic manifest transitions, minimal initialization, and guarded private-data lifecycle. |
| `upgrades/` | Release resolution, migration planning, targeted backup, uv handoff, recovery orchestration, and lifecycle reporting; never subsystem schema implementation. |
| `config.py`, `project_scope.py`, `attachments.py`, `tool_contracts.py`, `owned_operation.py` | Shared boundary utilities; they do not own user workflows or subsystem persistence. |

## Dependency boundaries

- Agent turns depend on the `Provider` protocol, never a concrete adapter.
  Provider-specific code remains under `ricky.llm`.
- Non-interface packages do not import `ricky.interfaces`. Concrete messaging
  transports are constructed at an interface composition root and injected
  behind `MessageTransport`.
- Agent-driving chat and persistent-session surfaces use `AgentLoop` or a
  platform-neutral application service. `ricky ask` is the intentional narrow
  exception: it performs one provider request without a session, tools, memory,
  skills, or agent loop.
- `runtime.composition` constructs providers, capabilities, stores, and their
  owned resources. Capability construction receives an issued profile scope
  before it discovers resources. `ProjectScope` binds filesystem authority only;
  it never selects a resource root.
- Tools never call an LLM. A tool accepts validated typed input, performs one
  bounded operation, and returns a typed `ToolResult`.
- Browser control depends on Ricky-owned domain contracts under `ricky.browser`.
  Playwright objects remain private to that subsystem, and browser tools use the
  ordinary capability, permission, effect, and event paths. The complete
  browser contract is in [browser.md](browser.md).
- Protected-value consumers depend on the broker under `ricky.protected_values`; the broker never
  imports a consumer, interface, provider, or browser package. It exposes safe metadata and local
  materialization, never a generic raw-value tool. The complete contract is in
  [protected-values.md](protected-values.md).
- Skills are passive prompt and resource bundles. They cannot register code,
  grant authority, or bypass tool dispatch.
- Tool declarations are the source of capability membership and operational
  metadata. Runners canonicalize arguments and apply permission or contract
  checks before dispatch; `ToolRegistry.dispatch()` alone is not an
  authorization boundary.
- `agent.context.assemble_context()` is the canonical request builder for an
  ordinary `AgentLoop` turn. One-shot `ask`, context compaction, and isolated
  workflow model tasks intentionally use smaller specialized request builders.
- Canonical user input is ordered text and typed media parts. Existing text-only
  interfaces adapt strings at their application boundary. Canonical media
  references contain immutable content identity and source labels, never bytes,
  paths, provider identity, producer details, or retention policy.
- Session media admission and retention belong to `ricky.media`. A runtime binds
  media materialization to the exact session, profile scope, provider, and
  current disclosure policy before injecting that resolver into a provider.
  Provider adapters encode validated bytes but never locate artifacts or decide
  source policy.
- Workflows run beside the ordinary agent loop. Their scheduler uses isolated
  model-task helpers and the shared tool, permission, and event path; it does
  not drive graph steps through `AgentLoop.run_turn()`.
- Each durable subsystem owns its schema, transactions, leases, recovery, and
  pruning. Gateway operations call public store APIs and never reach into
  another subsystem's SQL.
- Each durable subsystem also owns its upgrade adapter and all transforms of
  its SQL or serialized formats. The upgrade coordinator discovers and groups
  physical targets, orders adapters, hashes and journals the plan, backs up
  declared mutable state, and verifies completion; it never embeds another
  subsystem's migration logic. Ordinary store opening creates absent current
  state or validates existing state and does not lazily migrate it.

## Profiles, configuration, and storage

Every runtime receives an immutable `ProfileScope` before provider selection,
context assembly, capability discovery, or scoped store access. Every scope
contains `shared`; child work may preserve or narrow a scope but never widen
it. Provider choice does not grant profile access. Durable resources use
qualified `profile/name` identities wherever local names could shadow.

Read all configuration through `RickySettings` and helpers in `ricky.config`.
The installation configuration lives at `<user_data_dir>/ricky.toml`.
Profile-owned configuration and secrets live below
`<user_data_dir>/profiles/<profile>/`. Configuration values and infrastructure
credentials are file-backed. A host-local XDG bootstrap pointer selects one
immutable `user_data_dir` before those files are read; `RICKY_USER_DATA_DIR` is
the sole Ricky-specific bootstrap environment variable. `XDG_CONFIG_HOME`
selects the host configuration root rather than providing a Ricky setting.
Process and storage settings remain installation-owned. The complete software,
bootstrap, initialization, and removal contract is in
[installation.md](installation.md).

The two repository and runtime roots have different purposes:

- `src/ricky/builtins/` contains the commit-safe skills, workflows, and jobs
  distributed with Ricky. It is product surface, read-only at runtime, and
  resolved from the installed package rather than the working directory. See
  [builtins.md](builtins.md).
- `user_data_dir` contains configuration, credentials, generated state,
  transcripts, checkpoints, attachments, caches, and databases. All paths are
  resolved through `ricky.config`, never hardcoded by a subsystem.

The externally installed application environment is a third, separate owner.
Released software is installed and removed by `uv tool`; application code and
versioned dependencies never live below `user_data_dir` or project `.ricky/`.
Upgrade coordinates uv using a descriptor-bound exact wheel and immutable
dependency-constraints artifact. Managed service and schedule definitions bind
the exact absolute console script from that tool environment and do not depend
on PATH, an arbitrary uv executable, or a source checkout.

Every stateful installed process holds the shared installation operation lock
for its lifetime and passes the bootstrap identity, clean-migration-state, and
data-generation compatibility gate before loading configuration or opening
durable state. Lifecycle mutation and recovery hold the bounded exclusive form.
Help, version, decommission, guarded purge, resume, and rollback remain
available through narrow paths when the ordinary gate is closed. Because the
gateway holds a shared lifetime lock, an upgrader verifies and stops the exact
owned launcher before exclusive acquisition, waits for it to drain, then
acquires exclusive and rechecks ownership and inactivity.

Software replacement preserves exclusion by passing the already-held lock file
descriptor only to the verified target executable. The child validates the
descriptor, operation journal, target version, launcher, pointer, and
installation identity and assumes the same lock without a close/reacquire gap.
The complete manifest, lock, release, backup, rollback, and launcher contract
is in [installation.md](installation.md).

Profile-owned state uses its profile root. Installation-wide and multi-profile
stores may remain directly below `user_data_dir`, but derived records carry a
`ProfileScope` or `ProfileLabel` and every owning store enforces it on reads,
writes, lists, correlations, recovery, audit, and retention. Persistent-storage
tests use distinct `user_data_dir` and `project_data_dir` roots and assert which
root was touched.

Agent-usable protected values are distinct from infrastructure credentials. They use a
profile-local encrypted broker store, safe profile-qualified references, destination policy, and
purpose-built consumer operations. Infrastructure credentials remain configuration- or
subsystem-owned and are never automatically imported into that catalog.

The complete profile contract is in [profiles.md](profiles.md).

## Sessions, context, and events

`AgentSession` is the canonical serializable state for an agent conversation:
messages, model selection, profile scope, local task plan, ephemeral permission
grants, active skill or workflow invocation, text-artifact and private media
manifests, compaction checkpoints, and usage. Message history carries only
provider-neutral media references; raw media never enters session JSON.

Interactive chat may keep a session resident. Persistent surfaces fence and
revision session state through `SessionStore` and construct one bounded
`SessionRuntime` per turn. Runtime resources and permission grants are not
promoted into durable unattended authority.

Gateway foreground turns do not directly own browsers. Gateway browser work is
performed by an explicitly compiled background execution whose runtime owns the
browser for the complete execution attempt. Interactive CLI runtimes may own a
browser for the resident chat. Attached browser processes remain externally
owned even when Ricky owns their connection.

A gateway-owned ad hoc execution may park one prepared browser transaction for
an authenticated durable approval while retaining its browser, resource lease,
claim, and in-memory prepared effect. Parking suspends model and browser work;
only the fenced approval watcher remains active. The approval record binds one
exact live occurrence but never serializes the prepared effect. Runtime,
browser, lease, or process loss invalidates that occurrence, and recovery must
not reconstruct or dispatch it. Named and scheduled jobs expose only the
bounded read-oriented browser surface until they have a separately reviewed
protected-value and commit lifecycle.

Ordinary context sources—persona, accessible profile and resource catalogs,
memory indexes, active skill instructions, tool definitions, history, and the
pending input—enter agent requests through `agent.context`. Lossless tool-result
offloading uses session-owned artifact references. Semantic compaction replaces
only the active projection of a complete historical prefix; canonical history
and prior checkpoints remain evidence. Context projection independently bounds
image count, bytes, pixels, and estimated image tokens. Compaction uses fixed
metadata-only omission markers and never materializes pixels.

Workflow model and agent steps do not inherit ordinary chat context. They
receive only declared inputs, a result schema, an optional named skill body,
and, for agent steps, the compiled read-only tool set.

Typed `AgentEvent` values are runtime observations consumed by interfaces and
transcript writers. They are not an event list on `AgentSession`; each durable
surface explicitly owns the evidence it persists. The event union in
`agent.events` is authoritative.

## Tools, authority, and effects

Tool arguments are normalized and strictly validated before permission,
guardrail evaluation, effect identity, or execution. Tool metadata declares
risk, capability membership, effect kind, unattended eligibility, and any
required state guard. The complete declaration and testing contract is in
[tool-authoring.md](tool-authoring.md).

Interactive authority comes from ordered policy and explicit session grants; a
matching deny remains a ceiling. Session grants are inspectable, revocable, and
ephemeral. Background authority comes from exact compiled execution contracts,
job policy, current capability policy, profile scope, budgets, and effect
coordination. Interactive grants are never copied into background work.

Every external-effect tool provides a deterministic `EffectIdentity` before
dispatch and returns an `EffectReceipt`. When identity depends on mutable input,
a prepared-effect boundary freezes the exact payload used for review,
reservation, identity, and dispatch. Unattended runners reserve durable effect
identity and budget before calling the provider.

The foreground agent permission path also prepares external effects exactly
once after static denial and argument validation but before interactive review.
The permission preview may use the prepared object's safe summary, and dispatch
must receive that same immutable object. Prepared bytes are never persisted in
events or session state and are dropped on denial or cancellation.

A background browser commit uses a distinct durable approval boundary. The
capability-owned tool prepares once, the execution durably records a safe
digest and live binding, and the same owner waits without reserving or
dispatching the effect. A matching source-bound approval wakes that owner,
which rechecks the contract, authority, policy, claim, page, origin,
destination, target, envelope, and protected-source facts before reserving and
dispatching the same prepared object. Denial, expiry, cancellation, or any
binding loss proves `not_performed`; loss after reservation or possible
dispatch remains `in_doubt`. A durable approval cannot authorize a replacement
browser occurrence.

Retry follows evidence:

- `performed` is an absolute no-replay boundary.
- `in_doubt`, an ambiguous response, or interruption after possible dispatch is
  never retried automatically.
- `not_performed` is retryable only when the owning compiled contract explicitly
  defines safe replay and a stable idempotency key.
- Provider adapters retry only classified transient failures before any stream
  output becomes observable.

Cancellation is a state transition, not only task interruption. Owners cancel
and join child work and leave stores or protocols valid. Lost leases cancel and
await the owned operation. Persistent work becomes `cancelled` only when
durable evidence shows nothing observable; otherwise it becomes `uncertain` or
`in_doubt`.

## Durable automation and messaging

- A session task plan is model-maintained working state. A durable task is a
  separately stored, profile-owned responsibility with lifecycle, activity,
  leases, and artifacts. Neither is workflow state.
- A workflow owns its typed graph, step records, effect journal, checkpoint,
  and resume rules. A job may invoke one workflow but retains the outer launch,
  schedule, budget, standing-authority, overlap, lineage, and notification
  envelope. See [workflows.md](workflows.md).
- A schedule is desired trigger state for a named job. Refreshing validation
  evidence does not widen its approved authority envelope.
- An execution request is a fenced attempt under an immutable contract, not a
  second task system. Delegated authority can narrow the runnable contract but
  cannot add tools or profiles.
- Linked task context is explicitly a historical snapshot at dispatch. Later
  task reads and mutation results supersede its mutable state; the contract's
  pinned task revision continues to identify the authorization boundary. A
  snapshot omits lease credentials and never asserts current lease ownership.
- A gateway-owned execution can enter a fenced
  `awaiting_transaction_approval` state while its worker remains alive. The
  store owns the approval occurrence, TTL, wakeup, invalidation, and safe
  evidence; the runtime owns all live browser and prepared-effect state. A
  process restart invalidates, rather than resumes, that state.
- Notification producers submit immutable requests to logical routes. The
  notification store owns deduplication and the durable outbox; transport
  adapters own platform wire conversion and receipts, not producer policy.
- `ricky.messaging` owns the durable inbox, transport cursors and leases, exact
  outbound parts, and delivery orchestration. Concrete adapters cannot invoke
  providers, tools, jobs, executions, or the agent loop.
- The gateway coordinates persistent conversations and bounded foreground
  turns. It also composes recovery, health, audit, and retention through each
  subsystem's public API. The external supervisor restarts the process; Ricky's
  stores determine safe recovery.

## Changing the architecture

Do not append a historical decision log. Before changing a cross-cutting
boundary, obtain maintainer approval, then update this current contract and any
applicable subsystem design. Keep rationale only when it helps a future agent
preserve or deliberately revise an invariant. Git remains the source for
superseded designs and implementation history.
