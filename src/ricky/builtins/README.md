# Bundled resources

This directory holds the skills, workflows, and jobs that ship with Ricky. It is
product surface: a change here is a release change, reviewed and versioned like
code.

## Layout

- `skills/<name>/SKILL.md` — prompt skill bundles available in every
  installation.
- `workflows/<name>/workflow.toml` — version 2 workflow bundles.
- `jobs/<name>/job.toml` — version 3 job bundles.

A bundle directory name must match the `name` declared inside the bundle.

## Identity and precedence

Bundled resources have no owning profile. They carry the reserved `bundled`
owner and the qualified identity `bundled/<name>`.

Discovery reads user roots below `<user_data_dir>/profiles/<profile>/` first,
then this root. A user resource with the same bare name takes precedence over a
bundled resource. The bundled resource stays reachable by its qualified
`bundled/<name>` identity.

## Boundaries

- Ricky never writes below this root. It is read-only at runtime and is replaced
  wholesale by a release.
- No secrets, no user data, and no machine-issued credentials belong here.
- Personal or repository-specific resources belong in a profile's user root.
- Bundle contents are declarative. Executable capabilities live under
  `src/ricky/tools/`; a skill composes them by asking the agent to call
  registered tools.

See `.designs/builtins.md` for the durable contract.
