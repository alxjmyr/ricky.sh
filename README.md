# Ricky.sh

An agentic personal assistant. Like most people named Ricky... He's fine, but not exceptional...
Guaranteed to be marginally more productive than a drunk guy in a Canadian trailer park.

![Ricky](ricky.jpg)

## Status
Its a work in progress... 

See [the architecture](.designs/architecture.md) for the current system design
and engineering boundaries.

## User documentation

Start with the [Ricky documentation](docs/README.md) for installation, everyday use,
integrations, automation, persistent messaging, and operations.

But really... don't bother. Ricky is pretty self aware... 
Just holler at him... "Yo... Ricky, make the google accounts work for me. Make no mistakes". And he'll mostly figure it out.


## Install it

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

Start with the [contributor guide](docs/development.md) to find behavior owners,
CLI modules, and relevant tests. [AGENTS.md](AGENTS.md) defines the required
development commands and completion checks.

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
