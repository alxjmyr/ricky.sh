"""Tests for the interactive release helper."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_release.sh"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    fake_bin = tmp_path / "bin"
    repo.mkdir()
    fake_bin.mkdir()
    (repo / "scripts").mkdir()
    shutil.copy2(SCRIPT, repo / "scripts" / "run_release.sh")

    _run("git", "init", "--bare", str(remote), cwd=tmp_path)
    _run("git", "init", "-b", "main", cwd=repo)
    _run("git", "config", "user.name", "Release Test", cwd=repo)
    _run("git", "config", "user.email", "release@example.test", cwd=repo)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "ricky"\nversion = "0.6.0"\n', encoding="utf-8"
    )
    (repo / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "ricky"\nversion = "0.6.0"\n',
        encoding="utf-8",
    )
    _run("git", "add", ".", cwd=repo)
    _run("git", "commit", "-m", "initial", cwd=repo)
    _run("git", "remote", "add", "origin", str(remote), cwd=repo)
    _run("git", "push", "-u", "origin", "main", cwd=repo)

    uv = fake_bin / "uv"
    uv.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$UV_COMMAND_LOG"
if [[ $1 == version && $2 == --short ]]; then
    sed -n 's/^version = "\\([^"]*\\)"/\\1/p' pyproject.toml
elif [[ $1 == version && $2 == --bump && ${4:-} == --dry-run ]]; then
    echo "0.6.1"
elif [[ $1 == version && $2 == --bump ]]; then
    sed -i 's/0.6.0/0.6.1/g' pyproject.toml uv.lock
elif [[ $* == "run pytest" && ${UV_FAIL_PYTEST:-} == 1 ]]; then
    exit 9
fi
""",
        encoding="utf-8",
    )
    uv.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["UV_COMMAND_LOG"] = str(tmp_path / "uv-commands.log")
    return repo, env, Path(env["UV_COMMAND_LOG"])


def _run_interactive(
    repo: Path, env: dict[str, str], answers: str
) -> subprocess.CompletedProcess[str]:
    command = [
        "script",
        "-qfec",
        f"bash {shlex.quote(str(repo / 'scripts' / 'run_release.sh'))}",
        "/dev/null",
    ]
    return subprocess.run(
        command,
        cwd=repo,
        env=env,
        input=answers,
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_prepares_commit_and_annotated_tag_without_push(tmp_path: Path) -> None:
    repo, env, command_log = _make_repo(tmp_path)

    result = _run_interactive(repo, env, "3\ny\nShip it\nn\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Current version: 0.6.0" in result.stdout
    assert "Proposed release: v0.6.1 (patch bump)" in result.stdout
    assert "Push skipped" in result.stdout
    assert _run("git", "log", "-1", "--format=%s", cwd=repo).stdout.strip() == (
        "v0.6.1 release prep"
    )
    tag_contents = _run(
        "git", "for-each-ref", "--format=%(contents)", "refs/tags/v0.6.1", cwd=repo
    ).stdout
    assert tag_contents.startswith("Ship it\n")
    assert _run("git", "status", "--porcelain", cwd=repo).stdout == ""
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "version --short",
        "version --bump patch --dry-run --short",
        "version --bump patch",
        "sync",
        "version --short",
        "run pytest",
        "run ruff check .",
        "run pyright",
    ]


def test_release_refuses_a_dirty_worktree_before_changing_version(tmp_path: Path) -> None:
    repo, env, command_log = _make_repo(tmp_path)
    (repo / "notes.txt").write_text("not committed\n", encoding="utf-8")

    result = _run_interactive(repo, env, "")

    assert result.returncode != 0
    assert "working tree is not clean" in result.stdout
    assert not command_log.exists()
    assert 'version = "0.6.0"' in (repo / "pyproject.toml").read_text(encoding="utf-8")


def test_failed_validation_restores_version_files(tmp_path: Path) -> None:
    repo, env, _ = _make_repo(tmp_path)
    env["UV_FAIL_PYTEST"] = "1"

    result = _run_interactive(repo, env, "3\ny\n")

    assert result.returncode != 0
    assert "restoring version files" in result.stdout
    assert _run("git", "status", "--porcelain", cwd=repo).stdout == ""
    assert _run("git", "log", "-1", "--format=%s", cwd=repo).stdout.strip() == "initial"
    assert _run("git", "tag", cwd=repo).stdout == ""


def test_release_atomically_pushes_commit_and_tag(tmp_path: Path) -> None:
    repo, env, _ = _make_repo(tmp_path)
    remote = tmp_path / "remote.git"

    result = _run_interactive(repo, env, "3\ny\n\ny\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Pushed main and v0.6.1" in result.stdout
    local_head = _run("git", "rev-parse", "HEAD", cwd=repo).stdout
    remote_head = _run("git", "rev-parse", "refs/heads/main", cwd=remote).stdout
    assert remote_head == local_head
    assert _run("git", "rev-parse", "refs/tags/v0.6.1^{commit}", cwd=remote).stdout == local_head
