# Project operating manual

Ricky is a personal agentic assistant and harness built with Python 3.12+.
`pyproject.toml` is authoritative for dependencies and tool configuration. Use
`uv` exclusively; never use pip or Poetry.

## Reference material

- Read `.designs/architecture.md` before structural changes.
- Read the applicable current contract in `.designs/profiles.md`,
  `.designs/workflows.md`, `.designs/builtins.md`, or
  `.designs/tool-authoring.md` before changing that subsystem.
- Use `docs/README.md` as the user-documentation index. Read an item in
  `.designs/assets/` only when changing its integration.
- When present, `.plans/` contains active plans only and `.reviews/` unresolved
  findings only. Remove either when complete after preserving durable outcomes
  in designs, docs, code, and tests. Git history is not a current specification.

## Commands

Use these exact commands and flags:

| Purpose | Command |
|---|---|
| Install or sync | `uv sync` |
| All tests | `uv run pytest` |
| Inner-loop tests | `uv run pytest -m "not browser_integration"` |
| One test file | `uv run pytest tests/<file> -v` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Type check | `uv run pyright` |
| CLI | `uv run ricky` |

The sanctioned inner-loop test command excludes only tests that control a real
Playwright Chromium process. Browser unit, service, policy, backend-contract,
and tool tests still run. Use it while iterating when the current changes do not
affect the real-browser boundaries below. It is not a completion gate or a
substitute for the full test command.

During development, run
`uv run pytest tests/test_browser_integration.py -v` for browser changes that
can affect browser configuration or runtime composition, Playwright integration,
process/session/page ownership, live semantic snapshot shape or redaction,
action dispatch, navigation or destination interception, dialogs, frames or
popups, persistent resources, or CDP attachment.

Before declaring repository changes complete, all three must pass:
`uv run pytest && uv run ruff check . && uv run pyright`.

## Engineering rules

### Always

- Follow current architecture and directory patterns; keep modules
  single-purpose.
- For user-facing behavior changes, update `docs/` as needed. Keep user
  documentation concise, accurate, and aligned with Google developer
  documentation style.
- Add or update tests for behavior changes. Core APIs are async; use
  `asyncio.run()` only at interface entry points. Owners cancel and await tasks
  or subprocesses, close their resources, and test interruption wherever state
  may be partially mutated.
- Keep provider-specific code under `ricky.llm`. Non-interface packages never
  import `ricky.interfaces`.
- Define an idempotency boundary before retrying a stream or effect. Never retry
  after output or a side effect may be observable unless replay is explicitly
  safe.
- Use strict, JSON-round-trip-safe Pydantic models for serialized, durable,
  provider, tool, event, and configuration boundaries. Frozen dataclasses and
  protocols may represent in-process ownership and composition.
- Resolve configuration through `ricky.config` and typed `RickySettings` fields.
  Define defaults on settings models; document non-secret configuration in
  `ricky.toml.example` and secrets in `.secrets.toml.example`. Only
  `ricky.config` treats environment values as settings; adapters may only copy
  or sanitize child-process environments.
- Keep `src/ricky/builtins/` commit-safe and limited to the skills, workflows,
  and jobs distributed with Ricky. It is read-only at runtime; Ricky never
  writes below it. Ricky discovers no skills, workflows, or jobs from a project
  directory. Store live configuration and generated state under `user_data_dir`
  through `ricky.config` helpers; never hardcode either data root.
- For new persistent storage, test with distinct `user_data_dir` and
  `project_data_dir`, assert the intended root, and verify the other is
  untouched.
- Type secrets as `pydantic.SecretStr`. User-authored secrets come from the
  owning profile's `.secrets.toml` or the environment through `RickySettings`.
  Machine-issued credentials may persist only under their owning subsystem and
  profile.

### Ask first

- Adding a third-party production dependency.
- Changing an existing public API or database schema to fix a bug.
- Adopting or revising a cross-cutting architectural contract. After approval,
  update the current `.designs/` contract; do not append history.

### Never

- Read or edit `FUTURE.md`; it is a human-only scratchpad.
- Put a secret in `ricky.toml` or any committed file, or commit a live
  `.secrets.toml`.
- Print, log, or emit a secret. Unwrap `SecretStr` only at the outbound protocol
  boundary requiring it.
- Mix requested functional work with opportunistic global refactoring.
- Weaken a test merely to obtain a pass. If an intended behavior change requires
  different assertions, explain why and ask first.
- Claim a validation command passed unless you ran it successfully.
