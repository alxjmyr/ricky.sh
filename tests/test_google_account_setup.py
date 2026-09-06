"""Profile-owned Google account setup, privacy, and interrupted writes."""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import ricky.config as config
from ricky.installation import initialize_installation, write_private_file
from ricky.interfaces.cli import app as app_module
from ricky.interfaces.cli.app import app
from ricky.profiles.management import add_profile

runner = CliRunner()
_SECRET = "private-test-client-secret"


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "user-data"
    initialize_installation(root)
    add_profile("work")
    client_json = tmp_path / "client.json"
    client_json.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "client.apps.googleusercontent.com",
                    "client_secret": _SECRET,
                    "project_id": "test-project",
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    return root, client_json


def _add(client_json: Path, *, name: str = "mail", profile: str = "work") -> None:
    config.add_google_account(
        name, profile=profile, email="person@example.com", client_json=client_json
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_cli_add_creates_private_account_and_preserves_other_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, client_json = _setup(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    root_config = root / "ricky.toml"
    write_private_file(
        root_config, f'project_data_dir = "{project / "state"}"\n' + root_config.read_text()
    )
    owner = root / "profiles" / "work"
    write_private_file(
        owner / "ricky.toml", '# keep config comment\n[profile]\ndescription = "Work account"\n'
    )
    write_private_file(
        owner / ".secrets.toml", '# keep secret comment\nopenrouter_api_key = "existing-key"\n'
    )
    root_before = root_config.read_bytes()
    shared_before = _snapshot(root / "profiles" / "shared")

    result = runner.invoke(
        app,
        [
            "config",
            "google",
            "add",
            "mail",
            "--profile",
            "work",
            "--email",
            "person@example.com",
            "--client-json",
            str(client_json),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ricky config google auth work/mail" in result.output
    assert _SECRET not in result.output
    settings = config.load_settings()
    scope = settings.resolve_profile_scope("work")
    resolved = settings.resolve_profile_runtime_settings(scope)
    assert resolved.google.accounts["work/mail"].email == "person@example.com"
    assert resolved.google_oauth_clients["work/mail"].client_secret.get_secret_value() == _SECRET
    public = (owner / "ricky.toml").read_text()
    private = (owner / ".secrets.toml").read_text()
    assert "# keep config comment" in public
    assert "# keep secret comment" in private
    assert "existing-key" in private
    assert _SECRET not in public
    assert _SECRET not in root_config.read_text()
    assert stat.S_IMODE((owner / ".secrets.toml").stat().st_mode) == 0o600
    assert root_config.read_bytes() == root_before
    assert _snapshot(root / "profiles" / "shared") == shared_before
    assert not list(project.iterdir())


def test_add_rejects_duplicates_but_allows_same_name_in_other_profile(tmp_path: Path) -> None:
    root, client_json = _setup(tmp_path)
    _add(client_json)
    before = _snapshot(root / "profiles")
    with pytest.raises(ValueError, match="already exists"):
        _add(client_json)
    assert _snapshot(root / "profiles") == before
    _add(client_json, profile="shared")
    assert config.load_settings().profile_configs["shared"].google is not None


@pytest.mark.parametrize(
    "name,profile",
    [("mail", "missing"), ("work/mail", "work"), ("../mail", "work"), ("mail", "../work")],
)
def test_invalid_owner_or_name_does_not_mutate_profiles(
    tmp_path: Path, name: str, profile: str
) -> None:
    root, client_json = _setup(tmp_path)
    before = _snapshot(root / "profiles")
    with pytest.raises(ValueError):
        _add(client_json, name=name, profile=profile)
    assert _snapshot(root / "profiles") == before


@pytest.mark.parametrize(
    "payload",
    [
        '{"web":{"client_secret":"private-test-client-secret"}}',
        '{"installed":{"client_id":{},"client_secret":"private-test-client-secret"}}',
        '{"installed":{"client_secret":"private-test-client-secret"}}',
        "private-test-client-secret",
        "[]",
    ],
)
def test_invalid_client_is_redacted_and_does_not_write(tmp_path: Path, payload: str) -> None:
    root, client_json = _setup(tmp_path)
    client_json.write_text(payload)
    before = _snapshot(root / "profiles")
    result = runner.invoke(
        app,
        [
            "config",
            "google",
            "add",
            "mail",
            "--profile",
            "work",
            "--email",
            "person@example.com",
            "--client-json",
            str(client_json),
        ],
    )
    assert result.exit_code == 2
    assert "valid Google Desktop OAuth client" in result.output
    assert _SECRET not in result.output
    assert _snapshot(root / "profiles") == before


@pytest.mark.parametrize("write_number", [1, 2])
@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("existing_secrets", [False, True])
def test_failed_or_interrupted_write_restores_exact_originals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_number: int,
    failure_type: type[BaseException],
    existing_secrets: bool,
) -> None:
    root, client_json = _setup(tmp_path)
    if existing_secrets:
        write_private_file(
            root / "profiles" / "work" / ".secrets.toml",
            '# preserved\nopenrouter_api_key = "existing-key"\n',
        )
    before = _snapshot(root / "profiles")
    original_write = config.write_private_file
    writes = 0

    def fail_after_write(path: Path, content: str) -> None:
        nonlocal writes
        writes += 1
        original_write(path, content)
        if writes == write_number:
            raise failure_type("interrupted")

    monkeypatch.setattr(config, "write_private_file", fail_after_write)
    expected = KeyboardInterrupt if failure_type is KeyboardInterrupt else ValueError
    with pytest.raises(expected):
        _add(client_json)
    assert _snapshot(root / "profiles") == before
    config.load_settings()


@pytest.mark.parametrize("target", ["ricky.toml", ".secrets.toml", "profile", "profiles"])
def test_setup_refuses_symlinks(tmp_path: Path, target: str) -> None:
    root, client_json = _setup(tmp_path)
    owner = root / "profiles" / "work"
    path = (
        root / "profiles"
        if target == "profiles"
        else owner
        if target == "profile"
        else owner / target
    )
    outside = tmp_path / "outside"
    if path.exists():
        path.rename(outside)
    else:
        outside.write_text("")
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    before = _snapshot(outside) if outside.is_dir() else outside.read_bytes()
    with pytest.raises((ValueError, OSError)):
        _add(client_json)
    assert (_snapshot(outside) if outside.is_dir() else outside.read_bytes()) == before


def test_account_definition_in_secrets_is_not_adopted(tmp_path: Path) -> None:
    root, client_json = _setup(tmp_path)
    owner = root / "profiles" / "work"
    write_private_file(
        owner / ".secrets.toml", '[google.accounts.mail]\nemail = "original@example.com"\n'
    )
    before = _snapshot(owner)
    with pytest.raises(ValueError, match="already exists"):
        _add(client_json)
    assert _snapshot(owner) == before


def test_invalid_existing_secret_is_not_rendered(tmp_path: Path) -> None:
    root, client_json = _setup(tmp_path)
    write_private_file(
        root / "profiles" / "work" / ".secrets.toml",
        f'openrouter_api_key = {{ value = "{_SECRET}" }}\n',
    )
    result = runner.invoke(
        app,
        [
            "config",
            "google",
            "add",
            "mail",
            "--profile",
            "work",
            "--email",
            "person@example.com",
            "--client-json",
            str(client_json),
        ],
    )
    assert result.exit_code != 0
    assert _SECRET not in result.output


def test_add_requires_explicit_profile(tmp_path: Path) -> None:
    root, client_json = _setup(tmp_path)
    before = _snapshot(root / "profiles")
    result = runner.invoke(
        app,
        [
            "config",
            "google",
            "add",
            "mail",
            "--email",
            "person@example.com",
            "--client-json",
            str(client_json),
        ],
    )
    assert result.exit_code == 2
    assert "--profile" in result.output
    assert _snapshot(root / "profiles") == before


@pytest.mark.parametrize(
    "command", [[], ["google"], ["google", "auth", "work/mail"], ["google", "add"]]
)
def test_config_commands_hold_the_appropriate_installation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    _, client_json = _setup(tmp_path)
    modes: list[str] = []
    held: list[str] = []
    original_lock = config.installation_operation_lock
    original_load = app_module.load_settings
    original_write = config.write_private_file

    @contextmanager
    def track_lock(**kwargs: Any) -> Iterator[None]:
        assert not held
        modes.append(kwargs["mode"])
        with original_lock(**kwargs):
            held.append(kwargs["mode"])
            try:
                yield
            finally:
                held.pop()

    def load_under_shared(**kwargs: Any) -> config.RickySettings:
        assert held == ["shared"]
        return original_load(**kwargs)

    def fake_provider_command(factory: Any) -> None:
        assert held == ["shared"]

    def write_under_exclusive(path: Path, content: str) -> None:
        assert held == ["exclusive"]
        original_write(path, content)

    monkeypatch.setattr(app_module, "installation_operation_lock", track_lock)
    monkeypatch.setattr(config, "installation_operation_lock", track_lock)
    monkeypatch.setattr(app_module, "load_settings", load_under_shared)
    monkeypatch.setattr(app_module, "run_with_provider_errors", fake_provider_command)
    monkeypatch.setattr(config, "write_private_file", write_under_exclusive)
    adding = command == ["google", "add"]
    args = ["config", *command]
    if adding:
        args += [
            "mail",
            "--profile",
            "work",
            "--email",
            "person@example.com",
            "--client-json",
            str(client_json),
        ]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert modes == ["exclusive" if adding else "shared"]
    assert held == []


def test_add_refuses_an_active_runtime_lock_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, client_json = _setup(tmp_path)
    before = _snapshot(root / "profiles")
    original_lock = config.installation_operation_lock

    @contextmanager
    def immediate_lock(**kwargs: Any) -> Iterator[None]:
        kwargs["timeout_seconds"] = 0
        with original_lock(**kwargs):
            yield

    monkeypatch.setattr(config, "installation_operation_lock", immediate_lock)
    with original_lock(mode="shared", operation="test_runtime"):
        result = runner.invoke(
            app,
            [
                "config",
                "google",
                "add",
                "mail",
                "--profile",
                "work",
                "--email",
                "person@example.com",
                "--client-json",
                str(client_json),
            ],
        )
    assert result.exit_code == 1
    assert "could not start google_account_add" in result.output
    assert _snapshot(root / "profiles") == before


def test_rollback_failure_has_actionable_redacted_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, client_json = _setup(tmp_path)
    original_write = config.write_private_file
    calls = 0

    def fail_commit_and_restore(path: Path, content: str) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError(_SECRET)
        original_write(path, content)

    monkeypatch.setattr(config, "write_private_file", fail_commit_and_restore)
    result = runner.invoke(
        app,
        [
            "config",
            "google",
            "add",
            "mail",
            "--profile",
            "work",
            "--email",
            "person@example.com",
            "--client-json",
            str(client_json),
        ],
    )
    assert result.exit_code == 1
    assert "could not restore configuration" in result.output
    assert "before retrying" in result.output
    assert _SECRET not in result.output
    assert not (root / "profiles" / "work" / ".secrets.toml").exists()
