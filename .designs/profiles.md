# Profiles

This document defines the durable profile boundaries. User-facing setup and
commands belong in `docs/profiles.md`; canonical models live in
`ricky.profiles` and typed resolution lives in `ricky.config`.

## Purpose

A profile is both an agent environment and a data compartment. It may own
persona instructions, configuration, memory, capabilities, accounts,
credentials, tasks, downloads, temporary data, and other state.

`shared` is reserved and present in every runtime scope. Data placed there is
intentionally available to every Ricky context.

Profiles are not execution contracts. A profile limits which environment and
data a runtime may access; jobs, workflows, and execution contracts separately
define what work may run and with what authority. Provider identity never
selects or grants access to a profile.

## Canonical types

- `ProfileScope` is the immutable set of profiles issued to one runtime and its
  primary profile. It always includes `shared`, is canonically ordered and
  JSON-round-trip safe, and can only be narrowed for child work.
- `ProfileLabel` records every profile required to read derived durable data. A
  scope permits a label only when it contains all required profiles.
- `ProfileResourceRef` is the canonical `profile/name` identity for a resource
  owned by one profile. Qualified identity prevents same-named resources from
  shadowing one another.
- `ProfileRoutingDecision` records a bounded explanation of semantic routing
  within an already-issued scope. It grants no access.

Interfaces and deterministic application code issue a scope before selecting a
provider, discovering capabilities, assembling context, or opening scoped
stores. Model output, tool arguments, provider state, and resumed work must not
widen it. A child or delegated runtime receives the same scope or a narrower
one.

## Primary profile and routing

The primary profile supplies ordinary defaults, persona selection, and the
default destination for new user-authored data. Additional profiles make their
resources available without contributing persona or replacing primary
defaults. `shared` may itself be the primary profile. It is the only enabled
profile and the default in a newly initialized installation; settings,
persona, and other context from `shared` are applied exactly once in that
scope.

Inside the issued scope, choosing a profile is semantic routing rather than a
second permission gate. Ordinary writes default to the primary profile;
resource-derived writes default to the resource owner; deliberately universal
facts may target `shared`. Ask for clarification only when plausible choices
would materially change the result. Existing tool permission, authority, and
effect rules still govern the operation.

Data derived from multiple non-shared profiles must retain the complete source
label or use an explicit durable destination. It must never become readable by
a narrower scope merely because one resource was selected as the output target.

## Configuration and context

The fixed XDG bootstrap pointer owns installation identity and selects the
immutable `user_data_dir` before configuration is loaded.
`<user_data_dir>/ricky.toml` owns the enabled-profile registry, the default
profile, and process or storage mechanics. It does not persist or redirect
`user_data_dir`. Optional profile configuration and secrets live under:

```text
<user_data_dir>/profiles/<profile>/ricky.toml
<user_data_dir>/profiles/<profile>/.secrets.toml
```

Only `ricky.config` reads these files. Every setting remains a typed
`RickySettings` field, and secrets remain `SecretStr` values.
Before the first pointer exists, `RICKY_USER_DATA_DIR` may select the bootstrap
root. After initialization it may only confirm the bound root; a conflict fails
closed. No other Ricky setting or infrastructure credential is read from the
environment. `XDG_CONFIG_HOME` selects the host configuration directory that
contains the pointer and is not a Ricky setting. See
[installation.md](installation.md) for the complete lifecycle.

Resolution follows the data category rather than a generic merge:

- ordinary settings resolve primary, then shared, then installation defaults;
- provider policy is intersected across the entire scope before provider
  construction;
- capability exclusions and review requirements accumulate across the scope,
  while numeric ceilings use the strictest value;
- resource catalogs are the union of accessible profile-owned resources, with
  qualified identities;
- installation process and storage mechanics remain root-owned.

Context assembly exposes a compact catalog for only the issued profiles and a
non-secret catalog of accessible qualified accounts. Persona content is
prepended in this order: `shared/SOUL.md`, then the primary profile's
`SOUL.md` when the primary is not `shared`, then the harness-owned system
instructions. Accessible non-primary profiles do not contribute `SOUL.md`.

## Storage invariants

Profile-authored and profile-owned data lives below
`<user_data_dir>/profiles/<profile>/`. Generated state fixed to one profile
uses that profile root. State that may contain data from several profiles lives
in a scope namespace or a central owning store and carries a mandatory
`ProfileLabel`.

Scope enforcement belongs in each owning store and service, not only in an
interface. Read, write, list, correlation, attachment, recovery, audit, and
retention paths must all require a typed scope or qualified resource reference
and reject insufficient scopes.

Agent-usable protected values use profile-qualified identity and a profile-local encrypted store.
A scope may list or materialize only values whose owner it includes. Safe user-authored aliases and
descriptions may be model-visible, but raw values and value-derived hints never are. The `shared`
profile may own a protected value only under its existing intentional universal-sharing meaning.
Protected-value state is not configuration and never lives in `.secrets.toml` or project `.ricky/`.

Media derived from a configured browser resource is labeled with that
resource's owning profile. Media from an ephemeral browser is labeled with the
session's primary profile. Before a browser screenshot is captured for model
use, the source owner's profile policy must explicitly allow the session's
pinned provider; the same current policy is checked again at provider
materialization. That switch governs browser screenshots only. Other present or
future media producers own their own explicit admission policy without changing
canonical media identity.

The `src/ricky/builtins/` tree remains commit-safe, distributed capability
data. It carries the reserved `bundled` owner, which is never an enabled profile
and never names a data compartment. Profile-owned capabilities are visible only
when the runtime's profile scope permits their ownership, and a profile
definition takes precedence over a bundled one with the same bare name.
Generated runtime state never belongs in either tree. See
[builtins.md](builtins.md).

## Change checklist

When extending a profile-aware subsystem, preserve these properties:

1. Scope exists before provider, context, capability, or store construction.
2. `shared` is always present and child work cannot widen the parent scope.
3. Cross-boundary profile objects survive a JSON round trip.
4. Resource identity remains profile-qualified.
5. Derived records retain every source profile and stores enforce that label.
6. Tests use distinct `user_data_dir` and `project_data_dir` roots and prove
   generated state touches only the intended user-data location.
