"""Immutable release artifacts shared by the isolated installation drills."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

UV = shutil.which("uv")
ROOT = Path(__file__).parents[1]
SOURCE_INPUTS = (
    "src",
    "docs",
    "scripts/bundle_docs.py",
    "hatch_build.py",
    "pyproject.toml",
    "README.md",
    "ricky.toml.example",
    ".secrets.toml.example",
    "uv.lock",
)


@dataclass(frozen=True)
class Release:
    version: str
    artifacts: Path

    @property
    def wheel(self) -> Path:
        return self.artifacts / f"ricky-{self.version}-py3-none-any.whl"

    @property
    def constraints(self) -> Path:
        return self.artifacts / f"ricky-{self.version}-constraints.txt"

    @property
    def descriptor(self) -> Path:
        return self.artifacts / f"ricky-{self.version}-release.json"


@dataclass(frozen=True)
class BootstrapReleases:
    legacy: Release
    candidate: Release
    interrupted: Release


def copy_candidate_source(destination: Path) -> None:
    for name in SOURCE_INPUTS:
        source = ROOT / name
        target = destination / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _build(source: Path, artifacts: Path, environment: dict[str, str]) -> Release:
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    artifacts.mkdir()
    release = Release(version, artifacts)
    _run(
        [
            str(UV),
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--format",
            "requirements.txt",
            "--output-file",
            str(release.constraints),
        ],
        cwd=source,
        env=environment,
    )
    _run([str(UV), "build", "--wheel", "--out-dir", str(artifacts)], cwd=source, env=environment)
    _descriptor(artifacts, version, release.wheel, release.constraints)
    return release


def bootstrap_releases(root: Path, cache: Path) -> BootstrapReleases:
    """Build once across workers; publish readiness only after all builds succeed.

    The root belongs to one pytest invocation. A new run always builds the current
    working tree, including uncommitted source, lockfile, and documentation edits.
    Each recovery process still owns its installation and one-shot crash marker.
    """
    root.mkdir(exist_ok=True)
    candidate_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    releases = BootstrapReleases(
        Release("0.8.7", root / "legacy"),
        Release(candidate_version, root / "candidate"),
        Release(candidate_version, root / "interrupted"),
    )
    with (root / "build.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (root / "ready").exists():
            return releases
        build_home = root / "home"
        build_home.mkdir(exist_ok=True)
        environment = {
            **os.environ,
            "HOME": str(build_home),
            "XDG_CONFIG_HOME": str(build_home / "config"),
            "UV_CACHE_DIR": str(cache),
            "UV_NO_CONFIG": "1",
        }
        old_source = root / "source-legacy"
        old_source.mkdir()
        archived = subprocess.run(
            ["git", "archive", "v0.8.7", *SOURCE_INPUTS],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
            archive.extractall(old_source, filter="data")
        _build(old_source, releases.legacy.artifacts, environment)
        candidate_source = root / "source-candidate"
        copy_candidate_source(candidate_source)
        _build(candidate_source, releases.candidate.artifacts, environment)
        coordinator_path = candidate_source / "src/ricky/upgrades/orchestrator.py"
        coordinator = coordinator_path.read_text()
        needle = "    def _verify_target(self, journal: UpgradeJournal) -> None:\n"
        assert coordinator.count(needle) == 1
        coordinator_path.write_text(
            coordinator.replace(
                needle,
                needle + '        crash_marker = self._root / "interrupt-before-verification"\n'
                "        if crash_marker.exists():\n"
                "            crash_marker.unlink()\n"
                "            __import__('os')._exit(87)\n",
            )
        )
        _build(candidate_source, releases.interrupted.artifacts, environment)
        (root / "ready").touch()
        return releases


def _descriptor(
    root: Path,
    version: str,
    wheel: Path,
    constraints: Path,
) -> Path:
    def artifact(path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "name": path.name,
            "url": path.resolve().as_uri(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    path = root / f"ricky-{version}-release.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "repository": "alxjmyr/ricky.sh",
                "channel": "stable",
                "source": "local_drill",
                "software_version": version,
                "supported_source_data_generations": [1],
                "target_data_generation": 1,
                "python_requirement": ">=3.12",
                "minimum_uv_version": "0.6.0",
                "wheel": artifact(wheel),
                "constraints": artifact(constraints),
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        # Surface the child's own diagnosis; a bare CalledProcessError hides it.
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed
