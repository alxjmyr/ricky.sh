---
name: ricky-docs
description: Consult Ricky's installed user documentation to explain features, configure profiles and integrations, operate jobs and the gateway, or troubleshoot Ricky itself. Use for Ricky setup, usage, configuration, and recovery questions.
---

# Consult Ricky documentation

Use the documentation bundled with this release as the starting point for Ricky-specific
instructions. It is available offline and does not require a source checkout.

- Read `references/docs/README.md` with `read_skill_resource` for the guide index.
- Search an exact command, setting, error, or short phrase with `search_skill_resources`.
  Search is literal and case-insensitive; try a shorter term or synonym if needed.
  Results give a bundle-relative path and 1-based line number. Read the surrounding
  section with `read_skill_resource` before applying it. Page through more matches
  with `offset`, or narrow `path` to one file. An incomplete search is not proof of absence.
- `references/INDEX.md` lists this release's version and section locations. Use it to
  browse when you don't know the terminology.
- Configuration and troubleshooting start at `references/docs/configuration.md` and
  `references/docs/troubleshooting.md`. Operational recovery is in
  `references/docs/operations.md`.
- The complete examples are `references/ricky.toml.example` and
  `references/.secrets.toml.example`. Copy only needed settings; the configuration
  example demonstrates optional features and does not represent initialization defaults.

For diagnosis, inspect the relevant provider-free status command first. `ricky config`
shows redacted settings for the installation's default scope, not necessarily this
conversation's pinned scope. Check profile ownership and the affected runtime before
changing configuration. Resolve the actual data root; do not assume `~/.ricky`.

Use installed `ricky` commands. In a source checkout, follow its contributor instructions
for `uv run ricky`. Confirm available arguments with the installed command's `--help` if
the observed behavior differs. Do not substitute online instructions for a newer release
without checking version compatibility.

Explain the cause and smallest relevant change, perform authorized work through available
tools, then verify the result. The skill grants no extra tools or permissions. Gateway
and unattended runtimes may need the user to run a local command. Profile lifecycle or
upgrade commands can need an exclusive installation lock held by the current chat or
gateway; have the operator exit or stop the affected runtime before retrying locally.
Configuration changes generally need a new chat or gateway process to take effect; route
contract changes can also require `/new`.

Keep infrastructure credentials and vault passphrases out of conversation and tool output.
Use local hidden prompts or ask the user to enter credentials in the owning profile's
private file. Never dump `.secrets.toml`. Bundled references are read-only product files,
not live configuration. For an uncertain external effect, follow reconciliation guidance
instead of retrying it.

Cite the guide and section used. Distinguish documented behavior, observed diagnostic
evidence, and any remaining uncertainty. This skill cannot repair a startup or provider
failure that prevents Ricky from running; provide the documented local recovery commands.
