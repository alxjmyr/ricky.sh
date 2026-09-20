"""Target-release planning protocol and isolated-process ownership."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import ricky
from ricky.installation import initialize_installation, require_installation
from ricky.upgrades import planning
from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    AdapterTarget,
    MigrationPlan,
    MigrationStep,
    ReleaseArtifact,
    ReleaseDescriptor,
)
from ricky.upgrades.planning import (
    PlanningRequest,
    PlanningResult,
    TargetPlanningError,
    TargetReleasePlanner,
    inspect_target,
)
from ricky.upgrades.versions import ReleaseVersion


def _request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlanningRequest:
    root = tmp_path / "user-data"
    initialize_installation(root)
    pointer, manifest = require_installation()
    monkeypatch.setattr(ricky, "__version__", "0.8.9")
    return PlanningRequest(
        user_data_dir=pointer.user_data_dir,
        installation_id=pointer.installation_id,
        source_software_version=ReleaseVersion.parse("0.8.7"),
        target_software_version=ReleaseVersion.parse("0.8.9"),
        source_data_generation=manifest.data_generation,
        target_data_generation=1,
    )


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def test_target_inspection_is_read_only_and_keeps_absent_stores_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    project = tmp_path / "distinct-project"
    project.mkdir()
    before = _snapshot(tmp_path)
    result = inspect_target(request)
    assert PlanningResult.model_validate_json(result.model_dump_json()) == result
    assert result.plan.steps == ()
    assert any(item.state == "absent" for item in result.inventory)
    assert _snapshot(tmp_path) == before
    assert list(project.iterdir()) == []


@pytest.mark.parametrize("field", ["installation_id", "user_data_dir", "source_data_generation"])
def test_target_rejects_changed_installation_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    request = _request(tmp_path, monkeypatch)
    values: dict[str, object] = {
        "installation_id": "f" * 32,
        "user_data_dir": str(tmp_path / "other"),
        "source_data_generation": 2,
    }
    with pytest.raises(TargetPlanningError, match="identity or state"):
        inspect_target(request.model_copy(update={field: values[field]}))


def test_target_rejects_wrong_executable_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    monkeypatch.setattr(ricky, "__version__", "0.8.7")
    with pytest.raises(TargetPlanningError, match="executable"):
        inspect_target(request)


def test_target_rejects_descriptor_generation_unknown_to_its_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    with pytest.raises(TargetPlanningError, match="target data generation"):
        inspect_target(request.model_copy(update={"target_data_generation": 2}))


def test_planning_rejects_multi_step_owner_chain_before_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    path = str(Path(request.user_data_dir) / "store.sqlite3")
    target = AdapterTarget(
        adapter_id="test", target_id="store", path=path, physical_path=path, kind="sqlite"
    )
    steps = tuple(
        MigrationStep(
            adapter_id="test",
            target_id="store",
            step_id=f"v{version}",
            physical_path=path,
            source_schema_version=version,
            target_schema_version=version + 1,
        )
        for version in (1, 2)
    )
    before = _snapshot(Path(request.user_data_dir))
    with pytest.raises(ValueError, match="one owner step"):
        PlanningResult(
            request=request,
            plan=MigrationPlan.create(
                source_data_generation=1, target_data_generation=1, steps=steps
            ),
            inventory=(
                AdapterInspection(
                    target=target,
                    state="migration_required",
                    found_schema_version=1,
                    target_schema_version=3,
                    integrity_valid=True,
                    detail="old schema",
                ),
            ),
            preflights=(
                AdapterPreflight(target=target, estimated_backup_bytes=0, backup_paths=(path,)),
            ),
        )
    assert _snapshot(Path(request.user_data_dir)) == before


def _release(tmp_path: Path) -> tuple[ReleaseDescriptor, Path, Path]:
    wheel = tmp_path / "ricky-0.8.9-py3-none-any.whl"
    constraints = tmp_path / "ricky-0.8.9-constraints.txt"
    wheel.write_bytes(b"test-wheel")
    constraints.write_bytes(b"dependency==1.0\n")

    def artifact(path: Path) -> ReleaseArtifact:
        return ReleaseArtifact(
            name=path.name,
            url=path.as_uri(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size=path.stat().st_size,
        )

    return (
        ReleaseDescriptor(
            source="local_drill",
            software_version=ReleaseVersion.parse("0.8.9"),
            supported_source_data_generations=(1,),
            target_data_generation=1,
            python_requirement=">=3.12",
            minimum_uv_version=ReleaseVersion.parse("0.6.0"),
            wheel=artifact(wheel),
            constraints=artifact(constraints),
        ),
        wheel,
        constraints,
    )


def test_staging_is_private_and_response_is_bound_then_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, monkeypatch)
    result = inspect_target(request)
    release, wheel, constraints = _release(tmp_path)
    calls: list[tuple[list[str], dict[str, str]]] = []
    staged: list[Path] = []

    def run(command: list[str], *, environment: dict[str, str], **kwargs: Any) -> bytes:
        calls.append((command, environment))
        if command[1:3] == ["tool", "install"]:
            root = Path(environment["UV_TOOL_DIR"])
            staged.append(root.parent)
            python = root / "ricky" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            return b""
        assert kwargs["payload"] == request.model_dump_json().encode()
        return result.model_dump_json().encode()

    monkeypatch.setattr(planning, "_run", run)
    monkeypatch.setattr(planning, "_uv_executable", lambda: Path("/uv"))
    monkeypatch.setattr(planning, "discover_uv_version", lambda _: ReleaseVersion.parse("1.0.0"))
    monkeypatch.setenv("UV_TOOL_DIR", "/do-not-change-installed-tools")
    monkeypatch.setenv("SECRET_TOKEN", "do-not-pass")
    with TargetReleasePlanner(
        release=release, wheel=wheel, constraints=constraints, python=Path(sys.executable)
    ) as planner:
        assert planner.inspect(request) == result
        assert staged[0].is_dir()
    assert not staged[0].exists()
    assert calls[0][1]["UV_TOOL_DIR"] != "/do-not-change-installed-tools"
    assert all("SECRET_TOKEN" not in environment for _, environment in calls)
    assert calls[1][0][1:] == ["-I", "-m", "ricky.upgrades.planning"]


def test_process_failure_and_timeout_do_not_expose_child_output() -> None:
    with pytest.raises(TargetPlanningError, match="planning failed") as error:
        planning._run(
            [sys.executable, "-c", "import sys; print('secret-value'); sys.exit(2)"],
            environment={},
            timeout=5,
        )
    assert "secret-value" not in str(error.value)
    with pytest.raises(TargetPlanningError, match="timed out"):
        planning._run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            environment={},
            timeout=0,
        )


def test_interrupted_process_is_killed_and_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Process:
        def communicate(self, **kwargs: Any) -> None:
            raise KeyboardInterrupt

        def kill(self) -> None:
            events.append("kill")

        def wait(self) -> None:
            events.append("wait")

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    with pytest.raises(KeyboardInterrupt):
        planning._run(["target"], environment={}, timeout=1)
    assert events == ["kill", "wait"]


@pytest.mark.parametrize("failure", ["invalid_json", "wrong_request", "escaping_path"])
def test_client_rejects_invalid_or_unbound_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    request = _request(tmp_path, monkeypatch)
    result = inspect_target(request)
    release, wheel, constraints = _release(tmp_path)
    payload = result.model_dump(mode="json")
    if failure == "wrong_request":
        payload["request"]["installation_id"] = "f" * 32
    elif failure == "escaping_path":
        for item in (payload["inventory"][0], payload["preflights"][0]):
            item["target"]["physical_path"] = str(tmp_path / "foreign")
    import json

    response = b"invalid" if failure == "invalid_json" else json.dumps(payload).encode()
    monkeypatch.setattr(planning, "_run", lambda *args, **kwargs: response)
    planner = TargetReleasePlanner(
        release=release, wheel=wheel, constraints=constraints, python=Path(sys.executable)
    )
    planner._staged_python = Path(sys.executable)
    with pytest.raises(TargetPlanningError, match="response"):
        planner.inspect(request)


def test_failed_environment_creation_removes_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, wheel, constraints = _release(tmp_path)
    roots: list[Path] = []

    def fail(*args: Any, environment: dict[str, str], **kwargs: Any) -> bytes:
        roots.append(Path(environment["UV_TOOL_DIR"]).parent)
        raise KeyboardInterrupt

    monkeypatch.setattr(planning, "_run", fail)
    monkeypatch.setattr(planning, "_uv_executable", lambda: Path("/uv"))
    monkeypatch.setattr(planning, "discover_uv_version", lambda _: ReleaseVersion.parse("1.0.0"))
    with (
        pytest.raises(KeyboardInterrupt),
        TargetReleasePlanner(
            release=release, wheel=wheel, constraints=constraints, python=Path(sys.executable)
        ),
    ):
        pytest.fail("staging should have failed")
    assert len(roots) == 1
    assert not roots[0].exists()
