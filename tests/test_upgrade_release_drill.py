"""Isolated old-wheel to new-wheel uv tool upgrade drill."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
import tomlkit
from tomlkit.items import Table

UV = shutil.which("uv")


@pytest.mark.skipif(UV is None, reason="uv is required for the released-installation drill")
def test_isolated_uv_tool_replaces_itself_through_local_release_pair(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[1]
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    constraints_base = artifacts / "constraints.txt"
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
            str(constraints_base),
        ],
        cwd=repository,
    )

    descriptors: list[Path] = []
    wheels: dict[str, Path] = {}
    for version in ("0.6.0", "0.6.1"):
        source = tmp_path / f"source-{version}"
        shutil.copytree(repository / "src", source / "src")
        shutil.copy2(repository / "README.md", source / "README.md")
        project = tomlkit.parse((repository / "pyproject.toml").read_text(encoding="utf-8"))
        metadata = project["project"]
        assert isinstance(metadata, Table)
        metadata["version"] = version
        (source / "pyproject.toml").write_text(tomlkit.dumps(project), encoding="utf-8")
        _run(
            [str(UV), "build", "--wheel", "--out-dir", str(artifacts)],
            cwd=source,
        )
        wheel = artifacts / f"ricky-{version}-py3-none-any.whl"
        constraints = artifacts / f"ricky-{version}-constraints.txt"
        shutil.copy2(constraints_base, constraints)
        descriptors.append(_descriptor(artifacts, version, wheel, constraints))
        wheels[version] = wheel

    tool_root = tmp_path / "tools"
    bin_root = tmp_path / "bin"
    config_root = tmp_path / "config"
    user_root = tmp_path / "user-data"
    host_bin, systemctl_state, crontab_state = _fake_host_commands(tmp_path)
    environment = {
        **os.environ,
        "UV_TOOL_DIR": str(tool_root),
        "UV_TOOL_BIN_DIR": str(bin_root),
        "XDG_CONFIG_HOME": str(config_root),
        "PATH": os.pathsep.join((str(host_bin), str(bin_root), os.environ["PATH"])),
    }
    _run(
        [
            str(UV),
            "tool",
            "install",
            "--force",
            "--python",
            sys.executable,
            "--constraints",
            str(artifacts / "ricky-0.6.0-constraints.txt"),
            "--no-config",
            "--no-progress",
            str(wheels["0.6.0"]),
        ],
        env=environment,
    )
    executable = bin_root / "ricky"
    _run([str(executable), "init", "--user-data-dir", str(user_root)], env=environment)
    assert _run([str(executable), "--version"], env=environment).stdout.strip() == "ricky 0.6.0"

    # Every bundled resource must travel inside the wheel installed in an
    # environment that has no checkout of this repository.
    with zipfile.ZipFile(wheels["0.6.0"]) as archive:
        shipped = {name for name in archive.namelist() if name.startswith("ricky/builtins/")}
    builtins_root = repository / "src" / "ricky" / "builtins"
    expected_builtins = {
        f"ricky/builtins/{path.relative_to(builtins_root).as_posix()}"
        for path in builtins_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert expected_builtins <= shipped, sorted(shipped)
    assert not any("__pycache__" in name for name in shipped), sorted(shipped)

    project = tmp_path / "scheduled-project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'upgrade-drill'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    # The schedule still binds a project root for filesystem authority, but the
    # job itself is authored in the primary profile's user job root.
    # ``ricky init`` enables only the shared profile, which owns the job.
    job_dir = user_root / "profiles" / "shared" / "jobs" / "brief"
    job_dir.mkdir(parents=True, exist_ok=True)
    job_path = job_dir / "job.toml"
    job_path.write_text(_job_spec(), encoding="utf-8")
    _run(
        [
            str(executable),
            "schedule",
            "add",
            "brief",
            "--cron",
            "*/17 * * * *",
            "--project",
            str(project),
        ],
        cwd=project,
        env=environment,
    )
    _run([str(executable), "schedule", "sync"], cwd=project, env=environment)
    _run([str(executable), "gateway", "service", "install"], cwd=project, env=environment)
    _run([str(executable), "gateway", "service", "start"], cwd=project, env=environment)
    schedules_path = user_root / "schedules.toml"
    schedules_before = schedules_path.read_bytes()
    authored_job_before = job_path.read_bytes()
    cron_before = crontab_state.read_text(encoding="utf-8")
    assert "*/17 * * * *" in cron_before
    assert "schedule invoke" in cron_before
    state_before = json.loads(systemctl_state.read_text(encoding="utf-8"))
    assert state_before["active"] is True
    assert state_before["enabled"] is True

    upgraded = _run(
        [
            str(executable),
            "upgrade",
            "--yes",
            "--to",
            "0.6.1",
            "--release-descriptor",
            str(descriptors[0]),
            "--release-descriptor",
            str(descriptors[1]),
            "--json",
        ],
        cwd=project,
        env=environment,
        timeout=180,
    )

    result = json.loads(upgraded.stdout)
    assert result["status"] == "completed"
    assert result["source_software_version"] == "0.6.0"
    assert result["target_software_version"] == "0.6.1"
    assert result["managed"]["gateway"] == "restarted"
    assert len(result["managed"]["schedules_installed"]) == 1
    assert result["managed"]["schedules_approval_required"] == []
    assert _run([str(executable), "--version"], env=environment).stdout.strip() == "ricky 0.6.1"
    manifest = json.loads((user_root / "installation.json").read_text(encoding="utf-8"))
    assert manifest["migration_state"] == "clean"
    assert manifest["operation_id"] is None
    assert manifest["last_lifecycle_version"] == "0.6.1"
    assert Path(result["backup_manifest"]).is_file()
    assert schedules_path.read_bytes() == schedules_before
    assert job_path.read_bytes() == authored_job_before
    schedule_payload = tomllib.loads(schedules_before.decode("utf-8"))
    assert schedule_payload["schedules"][0]["cron"] == "*/17 * * * *"
    assert schedule_payload["schedules"][0]["enabled"] is True
    cron_after = crontab_state.read_text(encoding="utf-8")
    assert "*/17 * * * *" in cron_after
    assert "schedule invoke" in cron_after
    installed_executable = (tool_root / "ricky" / "bin" / "ricky").resolve()
    assert str(installed_executable) in cron_after
    unit = config_root / "systemd" / "user" / "ricky-gateway.service"
    assert f"ExecStart={installed_executable} gateway run" in unit.read_text(encoding="utf-8")
    state_after = json.loads(systemctl_state.read_text(encoding="utf-8"))
    assert state_after["active"] is True
    assert state_after["enabled"] is True
    assert state_after["calls"][-5:] == [
        "stop",
        "daemon-reload",
        "enable",
        "start",
        "is-active",
    ]


def _job_spec() -> str:
    return """version = 3
name = "brief"
description = "Release drill schedule."
provider = "openrouter"
model = "anthropic/claude-sonnet-4"
goal = "Report safely."
[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 123
effect_calls = 0
[tools]
allow = []
"""


def _fake_host_commands(tmp_path: Path) -> tuple[Path, Path, Path]:
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    systemctl_state = tmp_path / "systemctl-state.json"
    crontab_state = tmp_path / "crontab.txt"
    systemctl_state.write_text(
        json.dumps({"active": False, "enabled": False, "calls": []}),
        encoding="utf-8",
    )
    systemctl = host_bin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

state_path = Path("""
        + repr(str(systemctl_state))
        + """)
state = json.loads(state_path.read_text(encoding="utf-8"))
args = [item for item in sys.argv[1:] if item != "--user"]
action = args[0]
state["calls"].append(action)
code = 0
if action == "start":
    state["active"] = True
elif action == "stop":
    state["active"] = False
elif action == "enable":
    state["enabled"] = True
elif action == "disable":
    state["enabled"] = False
elif action == "is-active":
    print("active" if state["active"] else "inactive")
    code = 0 if state["active"] else 3
elif action == "is-enabled":
    print("enabled" if state["enabled"] else "disabled")
    code = 0 if state["enabled"] else 1
state_path.write_text(json.dumps(state), encoding="utf-8")
raise SystemExit(code)
""",
        encoding="utf-8",
    )
    crontab = host_bin / "crontab"
    crontab.write_text(
        """#!/usr/bin/env python3
import sys
from pathlib import Path

state_path = Path("""
        + repr(str(crontab_state))
        + """)
if sys.argv[1:] == ["-l"]:
    if not state_path.exists():
        print("no crontab for test-user", file=sys.stderr)
        raise SystemExit(1)
    print(state_path.read_text(encoding="utf-8"), end="")
    raise SystemExit(0)
state_path.write_text(Path(sys.argv[1]).read_text(encoding="utf-8"), encoding="utf-8")
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    crontab.chmod(0o755)
    return host_bin, systemctl_state, crontab_state


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
