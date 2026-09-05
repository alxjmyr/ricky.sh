# Ricky.sh

![Ricky](ricky.jpg)

A personal agentic assistant and agent harness — a cleanly implemented mini agent CLI that will grow toward persistent, multi-interface agentic workflows.

## Status

See [the architecture](.designs/architecture.md) for the current system design
and engineering boundaries.

## User documentation

Start with the [Ricky documentation](docs/README.md) for installation, everyday use,
integrations, automation, persistent messaging, and operations.

Released Linux builds install as versioned GitHub wheels managed by `uv`:

```bash
uv tool install \
  https://github.com/alxjmyr/ricky.sh/releases/download/vX.Y.Z/ricky-X.Y.Z-py3-none-any.whl
ricky init
ricky setup
ricky chat
```

Replace `X.Y.Z` with an exact published release. `uv` owns the application
environment; Ricky owns only its bootstrap pointer and private user-data root.

## Development

```bash
uv sync                    # install dependencies into .venv
uv run ricky --help        # run the CLI
uv run ricky config        # show resolved configuration

uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run pyright             # type check
```

To prepare a release from a clean, up-to-date branch, run:

```bash
bash ./scripts/run_release.sh
```

The script selects a semantic-version bump, refreshes `uv.lock`, runs the full
test, lint, and type-check gates, commits the version files, creates an
annotated `vX.Y.Z` tag, and optionally pushes the commit and tag atomically.

## Configuration

Configuration is **file-first** below `user_data_dir` (default: `~/.ricky`):

- `<user_data_dir>/ricky.toml` contains the profile registry and
  installation-wide non-secret settings.
- `<user_data_dir>/profiles/<name>/ricky.toml` contains profile-owned
  non-secret settings.
- `<user_data_dir>/profiles/<name>/.secrets.toml` contains credentials owned by
  that profile. Never commit this live file.

Initialize the minimal shared-only scaffold and configure it interactively:

```bash
ricky init
ricky setup
```

The XDG pointer at
`${XDG_CONFIG_HOME:-~/.config}/ricky/bootstrap.toml` is the sole persisted
authority for `user_data_dir`. Choose a custom root only during the first
`ricky init --user-data-dir PATH`; later commands reject conflicting root
selections. Settings and credentials are not read from general environment
variables. Run `ricky config` to inspect resolved settings; secrets are shown
only as set or not set.

### Google Auth Configuration
```
1. **On the remote** (where Ricky runs):

   ricky config google auth work --no-browser --callback-port 8676

   This prints a long `https://accounts.google.com/o/oauth2/v2/auth?...` URL. Don't open it yet.

2. **On your local machine**, set up the reverse tunnel:

   ssh -N \
    -o ExitOnForwardFailure=yes \
    -L 8676:127.0.0.1:8676 \
    <user>@<remote-host>
   This forwards connections to your local port 8676 back to the remote's `127.0.0.1:8676`, where Ricky's loopback server is listening.

3. **Now** open the consent URL from step 1 in your local browser. Google redirects to `http://127.0.0.1:8676?...`, which hits your local browser, tunnels back through SSH to the remote, and Ricky's listener picks up the auth code.
```

## LLM providers

Ricky supports OpenRouter, the Anthropic Messages API, and a local Claude Code
CLI. Choose and persist a default with `ricky config model`, or pin one
session with `--provider` / `--model`:

```bash
ricky ask -p claude_code -m sonnet "Reply exactly: ricky-ok"
ricky chat -p claude_code
```

Claude Code routing requires the `claude` CLI to be installed and logged into
the intended subscription. It uses the CLI as an LLM-only endpoint: Claude's
built-in tools, settings, MCP configuration, and project access are disabled;
Ricky retains tool dispatch, permissions, loop control, skills, and events. By
default, one isolated provider-native session is reused per Ricky session;
`resume_sessions = false` restores stateless full-transcript requests. Calls use
a neutral working directory, remove stray Anthropic API-key/token variables
from the subprocess environment, and never fall back silently to another
provider.
Claude Code has no CLI equivalents for canonical `temperature` or `max_tokens`
request fields, so this adapter intentionally ignores them.
