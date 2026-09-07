# Built-in resources

This document defines where Ricky finds skills, workflows, and jobs, and what
identity those resources carry. User-facing setup belongs in `docs/skills.md`,
`docs/workflows.md`, and `docs/jobs-and-schedules.md`. Discovery lives in
`ricky.skills.registry`, `ricky.workflows.registry`, and `ricky.jobs.registry`.
Capability identity lives in `ricky.capabilities.registry`.

## Purpose

Ricky ships with resources that must exist in every installation, independent of
where the process starts. The authoring skills that create jobs and workflows are
product capability, not repository content. They must be available to an
installed wheel with no checkout present.

Discovery therefore has exactly two roots: resources bundled inside the
distribution, and resources authored by a user below `user_data_dir`. There is
no project discovery root.

## Canonical roots

| Origin | Root | Writable | Lifecycle |
|---|---|---|---|
| `bundled` | `ricky/builtins/{skills,workflows,jobs}` inside the installed package | No | Replaced wholesale by a release |
| `user` | `<user_data_dir>/profiles/<profile>/{skills,workflows,jobs}` | Yes | Owned and migrated as user data |

The bundled root is the `ricky.builtins` package directory, resolved from the
module's own location. It is never derived from `find_project_root` and never
from a path relative to the working directory. A development tree and an
installed wheel present the same layout, so discovery has one code path.

The bundled root is read-only at runtime. No Ricky code writes below it. The
authoring skills create bundles in the primary profile's user root.

## Bundled user documentation

The `ricky-docs` skill bundles the release's user guides and configuration examples
as passive references. `docs/` and the repository's example TOML files are canonical;
the build hook generates the ignored reference copy and a versioned section index.
Normal and editable wheel builds use the same generator. The release action verifies
the actual wheel against those sources before publication. Runtime discovery never
copies or refreshes documentation. Developers refresh references explicitly after
editing docs in an existing editable checkout.

`search_skill_resources` searches only the active bundle with bounded literal text
matching. Like `read_skill_resource`, it uses the `builtin.skill.use` capability and
the registry's confined resource resolution. Pinned executions rebind both readers
to the selected immutable skill snapshot.

## Resource origin and identity

A bundled resource has no owning profile. Attributing one would be a fiction:
`ProfileResourceRef` exists to compartmentalize user data, and bundled content is
neither compartmentalized nor user data.

Resource identity therefore carries an explicit origin:

- A bundled resource is identified by its bare name and the `bundled` origin.
  Its qualified external form is `bundled/<name>`.
- A user resource keeps `ProfileResourceRef` and its `<profile>/<name>` form.

Origin is derived from the configured root that produced the resource, never
from path spelling. `ricky.capabilities.registry.derive_skill_owners` already
states this rule and must apply it to all three origins.

## Precedence and overrides

Discovery walks user roots first, then the bundled root. A user resource with the
same bare name shadows a bundled resource of that name. Shadowing is intentional:
it is how a user replaces a shipped skill.

Shadowing is silent in selection but never silent in inspection. Capability
inventory, chat's `/skill`, `ricky workflow list`, and `ricky job list` report
each resource's origin, and report a shadowed bundled resource as shadowed. A
caller reaches a shadowed bundled resource by its qualified `bundled/<name>` form.

Ambiguity across two enabled profiles remains an error, as today. Ambiguity
between a user resource and a bundled resource is not an error, because
precedence resolves it deterministically.

## Capability identity

Skill capability identifiers are `{origin}.skill.{profile}.{name}` for user
skills and `bundled.skill.{name}` for bundled skills. Bundled identifiers omit
the profile segment because bundled resources have no owning profile.

The owner token is `bundled`, not `builtin`. The capability namespace already
uses `builtin.*` for tool capabilities, including `builtin.skill.use`. A bundled
skill named `use` would otherwise produce exactly that identifier and collide
with the tool that activates skills.

`capability_requires_project_root` returns true only for tool capabilities in
`_PROJECT_ROOT_TOOL_IDS`. It no longer returns true for a skill, because no skill
is project-owned. This removes the requirement that a job selecting an authoring
skill must carry an exact project binding.

## Project scope after this change

`ProjectScope` remains. It binds filesystem authority: which directory
`read_file`, `list_dir`, `glob_search`, `grep_search`, `write_file`, `edit_file`,
and `run_shell` may operate within. That binding is unchanged, including the
gateway route `project_root` setting and the `--project` option.

`ProjectScope` no longer participates in discovery. The `include_project`
parameter is removed from every discovery entry point rather than defaulted,
because a silently defaulted flag is how implicit discovery survives.

`find_project_root` remains only for filesystem authority and attachments. It no
longer selects resource roots, so standing in an unrelated repository can no
longer change which skills, workflows, or jobs exist.

The repository's `.ricky/` directory is removed. Ricky does not read a project
directory for resources.

## Durable compatibility

Moving a resource into the bundled root changes its qualified identity, and
several durable surfaces record that identity.

**Workflow identity inside a job's resolved manifest.** A job that names a
workflow records `workflow.identity` among the source files that produce its
spec digest. A workflow that moves from a project root to the bundled root keeps
its content digest but changes identity from `<primary>/<name>` to
`bundled/<name>`, so the job's spec digest changes. A schedule approved against
the previous digest reports `lineage_required` and is omitted from managed cron
until its owner records a context decision. This is the intended fail-closed
path, not a defect: the schedule refuses to run rather than run against an
unreviewed definition. The owner resolves it by incrementing
`context.revision` to preserve prior context, or `context.lineage` to start
fresh, then re-approving and running `ricky schedule sync`. `ricky schedule
doctor` names the affected schedule.

Four durable surfaces record the retired `project` origin.

1. **Job spec digests** change for any job whose workflow moved roots, as
   described above.

2. **Execution contract snapshots** under
   `<user_data_dir>/executions/contracts/<digest>/` embed a copy of each skill
   bundle and record `project.skill.<profile>.<name>` in their capability set.
   Snapshots stay readable and stay valid evidence. A new contract for the same
   work produces different identifiers and therefore a different digest and a
   new snapshot. Historical snapshots are not rewritten.

3. **Pending execution drafts** record capability identifiers in
   `execution_drafts.requested_capabilities`. A draft that requested a
   project-owned skill capability names an identifier that no longer resolves,
   and `_selected_decisions` rejects it with `unknown or inactive capability`.
   That is fail-closed and legible, so no rewrite ships: a draft is transient
   per-conversation approval state, and the owner reissues it. No released
   version ever produced a `project.skill.*` identifier, so no installed base
   holds one.

4. **Workflow run checkpoints** carry `storage_scope: Literal["project", "user"]`
   with a `"project"` default. `WorkflowRunStore.root` already ignores this value
   and stores every run below `user_data_dir`, so the field is provenance only.
   The model continues to accept `"project"` so existing checkpoints load. Ricky
   stops producing it.

Authority grants are unaffected. They use the separate delegable effect
capability namespace, not capability registry identifiers.

Authored `job.toml` bundles are unaffected. Version 3 declares `[tools] allow` by
tool name and pins no capability identifier.

No surface above requires a data migration, so the data generation stays at 1.
Every affected surface either keeps loading unchanged, stays valid as historical
evidence, or fails closed with a message that names the recovery. A generation
bump would gate upgrades without protecting anything.

## Boundaries

- Bundled content is product surface. A change to a bundled skill is a release
  change, reviewed and versioned like code.
- Bundled content holds no secrets, no user data, and no machine-issued
  credentials.
- Personal or repository-specific resources belong in a profile's user root, not
  in the bundled root.
- Ricky never writes below the bundled root, and never treats a path below it as
  a migration target.
