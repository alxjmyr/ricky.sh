"""Isolated old-wheel to new-wheel uv tool upgrade drill."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
import tomlkit
from tomlkit.items import Table

from release_support import (
    UV,
    BootstrapReleases,
    _descriptor,
    _run,
    bootstrap_releases,
    copy_candidate_source,
)

pytestmark = pytest.mark.release_integration


@pytest.fixture(autouse=True)
def isolated_uv_cache(release_uv_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UV_CACHE_DIR", str(release_uv_cache))


@pytest.fixture(scope="session")
def bootstrap_artifacts(test_run_root: Path, release_uv_cache: Path) -> BootstrapReleases:
    return bootstrap_releases(test_run_root / "bootstrap-releases", release_uv_cache)


@pytest.mark.skipif(UV is None, reason="uv is required for the released-installation drill")
@pytest.mark.parametrize("recovery", ["complete", "resume", "rollback"])
def test_actual_087_schemas_upgrade_with_isolated_bootstrap_and_recovery(
    tmp_path: Path, recovery: str, bootstrap_artifacts: BootstrapReleases
) -> None:
    """Exercise old code, target planning, migration, interruption, and recovery.

    The source comes from the actual release tag and its own dependency lock,
    not candidate code with a relabeled version. Only the candidate contains a
    test-only one-shot crash immediately before whole-installation verification.
    """
    legacy = bootstrap_artifacts.legacy
    target = (
        bootstrap_artifacts.candidate if recovery == "complete" else bootstrap_artifacts.interrupted
    )
    candidate = target.version
    crash_marker = tmp_path / "user-data" / "interrupt-before-verification"
    descriptors = [legacy.descriptor, target.descriptor]
    host_bin, service_state, _ = _fake_host_commands(tmp_path)
    tool_root, bin_root = tmp_path / "tools", tmp_path / "bin"
    environment = {
        **os.environ,
        "UV_TOOL_DIR": str(tool_root),
        "UV_TOOL_BIN_DIR": str(bin_root),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PATH": os.pathsep.join((str(host_bin), str(bin_root), os.environ["PATH"])),
    }
    install = [
        str(UV),
        "tool",
        "install",
        "--force",
        "--python",
        sys.executable,
        "--no-config",
        "--no-progress",
    ]
    _run(
        [
            *install,
            "--constraints",
            str(legacy.constraints),
            str(legacy.wheel),
        ],
        env=environment,
    )
    bootstrap_environment = {
        **environment,
        "UV_TOOL_DIR": str(tmp_path / "bootstrap-tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bootstrap-bin"),
    }
    _run(
        [
            *install,
            "--constraints",
            str(target.constraints),
            str(target.wheel),
        ],
        env=bootstrap_environment,
    )
    bootstrap = tmp_path / "bootstrap-tools/ricky/bin/ricky"
    executable = bin_root / "ricky"
    user_root, project_root = tmp_path / "user-data", tmp_path / "project-data"
    project_root.mkdir()
    (project_root / "sentinel").write_text("project state must not change")
    _run([str(executable), "init", "--user-data-dir", str(user_root)], env=environment)
    python = tool_root / "ricky/bin/python"
    seeded = _run(
        [str(python), "-I", "-B", "-c", _LEGACY_SEED, str(user_root), str(project_root)],
        env=environment,
    )
    records = json.loads(seeded.stdout)
    _assert_legacy_records(user_root, records, migrated=False)
    disabled_profile = user_root / "profiles/archive"
    disabled_profile.mkdir()
    (disabled_profile / "ricky.toml").write_text("# Disabled profile settings.\n")
    _run([str(executable), "gateway", "service", "install"], env=environment)
    _run([str(executable), "gateway", "service", "start"], env=environment)
    config_before = (user_root / "ricky.toml").read_bytes()
    if recovery != "complete":
        crash_marker.touch()
    command = [
        str(bootstrap),
        "upgrade",
        "--bootstrap-from",
        str(executable),
        "--yes",
        "--to",
        candidate,
        "--json",
    ]
    for descriptor in descriptors:
        command += ["--release-descriptor", str(descriptor)]
    before_check = {path: path.read_bytes() for path in user_root.rglob("*") if path.is_file()}
    check_command = [argument for argument in command if argument != "--yes"] + ["--check"]
    checked = json.loads(_run(check_command, env=environment, cwd=tmp_path, timeout=240).stdout)
    assert checked["status"] == "update_available"
    assert checked["current_software_version"] == "0.8.7"
    assert {("sessions", 1, 2), ("executions", 8, 9)} <= {
        (step["adapter_id"], step["source_schema_version"], step["target_schema_version"])
        for step in checked["plan"]["steps"]
    }
    assert any("profiles/archive/" in item["target"]["path"] for item in checked["inventory"])
    assert before_check == {
        path: path.read_bytes() for path in user_root.rglob("*") if path.is_file()
    }
    interrupted = subprocess.run(
        command,
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    manifest = json.loads((user_root / "installation.json").read_text())
    if recovery == "complete":
        assert interrupted.returncode == 0, interrupted.stderr + interrupted.stdout
        completed = json.loads(interrupted.stdout)
        operation_id = completed["operation_id"]
    else:
        assert interrupted.returncode != 0, interrupted.stdout
        assert not crash_marker.exists(), interrupted.stderr + interrupted.stdout
        assert manifest["migration_state"] != "clean"
        operation_id = manifest["operation_id"]
    operation = user_root / "upgrades" / operation_id
    journal = json.loads((operation / "journal.json").read_text())
    steps = {
        (step["adapter_id"], step["source_schema_version"], step["target_schema_version"])
        for step in journal["ordered_steps"]
    }
    assert {("sessions", 1, 2), ("executions", 8, 9)} <= steps
    backup = json.loads((operation / "backup/manifest.json").read_text())
    backed_up = {item["source_path"] for item in backup["items"]}
    assert {
        str(user_root / "sessions/sessions.sqlite3"),
        str(user_root / "executions/executions.sqlite3"),
    } <= backed_up
    _assert_legacy_records(user_root, records, migrated=True)
    if recovery != "complete":
        recovery_command = [str(executable), "upgrade", "--" + recovery, "--json"]
        if recovery == "rollback":
            recovery_command.append("--yes")
        restored = json.loads(
            _run(recovery_command, env=environment, cwd=tmp_path, timeout=240).stdout
        )
        assert restored["status"] == ("completed" if recovery == "resume" else "rolled_back")
    _assert_legacy_records(user_root, records, migrated=recovery != "rollback")
    manifest = json.loads((user_root / "installation.json").read_text())
    assert manifest["migration_state"] == "clean" and manifest["operation_id"] is None
    version = "0.8.7" if recovery == "rollback" else candidate
    assert (
        _run([str(executable), "--version"], env=environment).stdout.strip() == f"ricky {version}"
    )
    assert json.loads(service_state.read_text())["active"] is True
    assert (user_root / "ricky.toml").read_bytes() == config_before
    assert not (user_root / "profiles/shared/protected-values").exists()
    assert (disabled_profile / "ricky.toml").read_text() == "# Disabled profile settings.\n"
    assert sorted(path.name for path in disabled_profile.iterdir()) == ["ricky.toml"]
    assert list(project_root.iterdir()) == [project_root / "sentinel"]
    assert (project_root / "sentinel").read_text() == "project state must not change"
    _run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            _LEGACY_RUNTIME_READBACK,
            str(user_root),
            str(project_root),
        ],
        env=environment,
    )


_LEGACY_SEED = """
import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.executions.store import ExecutionStore
from ricky.executions.types import ExecutionRequest
from ricky.profiles import ProfileScope
from ricky.sessions.store import SessionStore
from ricky.sessions.types import StoredTurn

async def seed():
    root = Path(sys.argv[1])
    settings = RickySettings(user_data_dir=str(root), project_data_dir=sys.argv[2])
    scope = ProfileScope.create('shared')
    store = SessionStore(settings)
    await store.initialize()
    session = AgentSession.create(settings, profile_scope=scope)
    await store.create(session, scope=scope)
    lease = await store.acquire(session.id, 'writer', scope=scope)
    turn = StoredTurn(id='legacy-turn', session_id=session.id, profile_label=scope.label(),
                     inbound_ref='legacy-inbound', base_revision=0, status='running',
                     started_at=datetime.now(UTC))
    await store.commit(lease, 0, session, turn)
    await store.release(lease)
    executions = ExecutionStore(settings)
    await executions.initialize()
    await executions.submit(ExecutionRequest(
        id='execution_' + 'a' * 32, kind='named_job', status='queued',
        named_job='shared/legacy', job_digest='b' * 64, profile_scope=scope,
        notification_route='none', request_key='legacy-key', created_at=datetime.now(UTC),
    ), scope=scope)
    with sqlite3.connect(store.db_path) as db:
        original = db.execute('SELECT session_json FROM sessions').fetchone()[0]
    print(json.dumps({'session_json': original}))

asyncio.run(seed())
"""


_LEGACY_RUNTIME_READBACK = """
import asyncio
import sqlite3
import sys
from pathlib import Path
from ricky.config import RickySettings
from ricky.executions.store import ExecutionStore
from ricky.profiles import ProfileScope
from ricky.sessions.store import SessionStore

async def check():
    settings = RickySettings(user_data_dir=sys.argv[1], project_data_dir=sys.argv[2])
    scope = ProfileScope.create('shared')
    sessions = SessionStore(settings)
    await sessions.initialize()
    with sqlite3.connect(Path(sys.argv[1]) / 'sessions/sessions.sqlite3') as db:
        identity = db.execute('SELECT id FROM sessions').fetchone()[0]
    assert (await sessions.get(identity, scope=scope)).session.id == identity
    turns = await sessions.turns(identity, scope=scope)
    assert len(turns) == 1 and turns[0].status == 'committed'
    assert turns[0].inbound_ref == 'legacy-inbound'
    executions = ExecutionStore(settings)
    await executions.initialize()
    request = await executions.get('execution_' + 'a' * 32, scope=scope)
    assert request.named_job == 'shared/legacy' and request.status == 'queued'
    assert request.request_key == 'legacy-key' and request.job_digest == 'b' * 64

asyncio.run(check())
"""


def _assert_legacy_records(root: Path, records: dict[str, str], *, migrated: bool) -> None:
    with sqlite3.connect(root / "sessions/sessions.sqlite3") as db:
        assert (
            db.execute("SELECT session_json FROM sessions").fetchone()[0] == records["session_json"]
        )
        assert db.execute("SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()[
            0
        ] == ("2" if migrated else "1")
        assert db.execute("SELECT id, status FROM turns").fetchall() == [
            ("legacy-turn", "committed")
        ]
        if migrated:
            assert db.execute(
                "SELECT background_handoffs, handoff_acknowledgement FROM turns"
            ).fetchone() == ("[]", None)
    with sqlite3.connect(root / "executions/executions.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == (9 if migrated else 8)
        assert db.execute("SELECT id, named_job FROM execution_requests").fetchall() == [
            ("execution_" + "a" * 32, "shared/legacy")
        ]


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
        copy_candidate_source(source)
        if version == "0.6.0":
            # A protocol-aware source still has only its own schema knowledge.
            # Use the real old owner implementation, not a changed version label.
            old_owner = _run(
                ["git", "show", "v0.8.7:src/ricky/sessions/upgrade.py"], cwd=repository
            ).stdout
            (source / "src/ricky/sessions/upgrade.py").write_text(old_owner)
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
        with zipfile.ZipFile(wheel) as archive:
            references = "ricky/builtins/skills/ricky-docs/references/"
            assert f"Ricky {version}" in archive.read(references + "INDEX.md").decode()
            assert (
                archive.read(references + "docs/configuration.md")
                == (repository / "docs/configuration.md").read_bytes()
            )
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
    installed_python = tool_root / "ricky" / "bin" / "python"
    original_session = _run(
        [str(installed_python), "-I", "-B", "-c", _DIRECT_LEGACY_SESSION_SEED, str(user_root)],
        cwd=tmp_path,
        env=environment,
    ).stdout.strip()
    _run(
        [str(installed_python), "-I", "-c", _DOCS_SMOKE, "0.6.0"],
        cwd=tmp_path,
        env=environment,
    )

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
    _run(
        [str(installed_python), "-I", "-c", _DOCS_SMOKE, "0.6.1"],
        cwd=tmp_path,
        env=environment,
    )
    manifest = json.loads((user_root / "installation.json").read_text(encoding="utf-8"))
    assert manifest["migration_state"] == "clean"
    assert manifest["operation_id"] is None
    assert manifest["last_lifecycle_version"] == "0.6.1"
    assert Path(result["backup_manifest"]).is_file()
    direct_backup = json.loads(Path(result["backup_manifest"]).read_text())
    assert str(user_root / "sessions/sessions.sqlite3") in {
        item["source_path"] for item in direct_backup["items"]
    }
    with sqlite3.connect(user_root / "sessions/sessions.sqlite3") as database:
        assert (
            database.execute("SELECT session_json FROM sessions").fetchone()[0] == original_session
        )
        assert (
            database.execute(
                "SELECT value FROM store_metadata WHERE key='schema_version'"
            ).fetchone()[0]
            == "2"
        )
        assert database.execute(
            "SELECT id, background_handoffs, handoff_acknowledgement FROM turns"
        ).fetchone() == ("legacy-turn", "[]", None)
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
    upgrade_calls = state_after["calls"][len(state_before["calls"]) :]
    assert [call for call in upgrade_calls if call not in {"is-active", "is-enabled"}] == [
        "stop",
        "daemon-reload",
        "enable",
        "start",
    ]
    assert upgrade_calls[-1] == "is-active"


_DIRECT_LEGACY_SESSION_SEED = """
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.profiles import ProfileScope
from ricky.sessions.upgrade import create_current_sessions_store, SCHEMA_VERSION

assert SCHEMA_VERSION == 1
root = Path(sys.argv[1])
settings = RickySettings(user_data_dir=str(root))
session = AgentSession.create(settings, profile_scope=ProfileScope.create('shared'))
path = root / 'sessions/sessions.sqlite3'
create_current_sessions_store(path)
payload = session.model_dump_json()
now = datetime.now(UTC).isoformat()
with sqlite3.connect(path) as db:
    db.execute('''INSERT INTO sessions
        (id, schema_version, session_json, revision, status, created_at, updated_at)
        VALUES (?, 1, ?, 0, 'active', ?, ?)''', (session.id, payload, now, now))
    db.execute('''INSERT INTO turns
        (id, session_id, base_revision, status, started_at, finished_at)
        VALUES ('legacy-turn', ?, 0, 'committed', ?, ?)''', (session.id, now, now))
print(payload)
"""


_DOCS_SMOKE = """
import asyncio
from pathlib import Path
import sys
import ricky
from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.skills.registry import discover_skills
from ricky.skills.search import SearchSkillResourcesTool
from ricky.skills.tool import ReadSkillResourceTool
from ricky.tools import ToolContext, ToolRegistry

assert Path(ricky.__file__).is_relative_to(Path(sys.prefix))

async def check():
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    skills = discover_skills(settings=settings, profile_scope=session.profile_scope)
    assert skills.activate(session, 'bundled/ricky-docs').ok
    tools = ToolRegistry([SearchSkillResourcesTool(skills), ReadSkillResourceTool(skills)])
    ctx = ToolContext(cwd=Path.cwd(), settings=settings, session=session)
    result = await tools.dispatch('search_skill_resources', {
        'query': 'schedule refresh', 'path': 'references/docs/jobs-and-schedules.md'
    }, ctx)
    assert not result.is_error and result.data['matches'], result.content
    index = await tools.dispatch('read_skill_resource', {
        'path': 'references/INDEX.md', 'limit': 1
    }, ctx)
    assert 'Ricky ' + sys.argv[1] in index.content, index.content

asyncio.run(check())
"""


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
