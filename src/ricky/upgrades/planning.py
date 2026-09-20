"""Read-only target-release planning in a disposable, isolated tool environment.

The calling lifecycle owns the installation lock. This protocol deliberately
does not acquire it again: it also runs while that caller holds it exclusively.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import ricky
from ricky.installation import require_installation
from ricky.upgrades.inventory import build_upgrade_registry
from ricky.upgrades.journal import sanitize_failure_summary
from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    MigrationPlan,
    ReleaseDescriptor,
)
from ricky.upgrades.software import (
    SoftwareReplacementError,
    _sanitized_environment,
    _uv_executable,
    _verify_artifact,
    discover_uv_version,
)
from ricky.upgrades.versions import CURRENT_DATA_GENERATION, ReleaseVersion

MAX_PLANNING_BYTES = 4 * 1024 * 1024
_PLANNING_TIMEOUT_SECONDS = 120


class TargetPlanningError(SoftwareReplacementError):
    """Target planning could not establish a verified, read-only plan."""


class PlanningFailure(BaseModel):
    """Bounded sanitized diagnostics from the private planning endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    format_version: Literal[1] = 1
    error: str = Field(min_length=1, max_length=500)


class PlanningRequest(BaseModel):
    """Exact installation and release identities supplied by the coordinator."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    format_version: Literal[1] = 1
    user_data_dir: str = Field(min_length=1, max_length=4096)
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_software_version: ReleaseVersion
    target_software_version: ReleaseVersion
    source_data_generation: int = Field(ge=1)
    target_data_generation: int = Field(ge=1)

    @field_validator("user_data_dir")
    @classmethod
    def _canonical_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("planning data root must be absolute and canonical")
        return value


class PlanningResult(BaseModel):
    """Target-owned inventory and migration plan, bound to the request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request: PlanningRequest
    plan: MigrationPlan
    inventory: tuple[AdapterInspection, ...]
    preflights: tuple[AdapterPreflight, ...]

    @model_validator(mode="after")
    def _bound_plan(self) -> Self:
        if (
            self.plan.source_data_generation != self.request.source_data_generation
            or self.plan.target_data_generation != self.request.target_data_generation
        ):
            raise ValueError("target plan generations differ from request")
        targets = [item.target for item in self.inventory]
        if len({(item.adapter_id, item.target_id) for item in targets}) != len(targets):
            raise ValueError("target inventory contains duplicate identities")
        if [item.target for item in self.preflights] != targets:
            raise ValueError("target preflights differ from inventory")
        root = Path(self.request.user_data_dir)
        paths = [path for target in targets for path in (target.path, target.physical_path)]
        paths.extend(path for item in self.preflights for path in item.backup_paths)
        if any(not Path(path).is_relative_to(root) for path in paths):
            raise ValueError("target planning paths escape the installation root")
        for item in self.inventory:
            if item.state in {"unsupported", "corrupt"} or not item.integrity_valid:
                raise ValueError("target inventory contains an invalid store")
            steps = [
                step
                for step in self.plan.steps
                if (
                    step.adapter_id == item.target.adapter_id
                    and step.target_id == item.target.target_id
                )
            ]
            if item.state != "migration_required":
                if steps:
                    raise ValueError("target plan mutates a store that requires no migration")
                continue
            version = item.found_schema_version
            if not steps or version is None:
                raise ValueError("target plan omits a required migration")
            if len(steps) != 1:
                raise ValueError("each target must have one owner step reaching its final schema")
            for step in steps:
                if step.source_schema_version != version:
                    raise ValueError("target migration schema chain is discontinuous")
                version = step.target_schema_version
            if version != item.target_schema_version:
                raise ValueError("target migration does not reach the required schema")
        for step in self.plan.steps:
            if not any(
                target.adapter_id == step.adapter_id
                and target.target_id == step.target_id
                and target.physical_path == step.physical_path
                for target in targets
            ):
                raise ValueError("target plan step has no inspected target")
        return self


def inspect_target(request: PlanningRequest) -> PlanningResult:
    """Inspect without opening ordinary stores or taking the caller's lock."""

    if ReleaseVersion.parse(ricky.__version__) != request.target_software_version:
        raise TargetPlanningError("planning executable does not match the target release")
    if request.target_data_generation != CURRENT_DATA_GENERATION:
        raise TargetPlanningError("planning executable does not match the target data generation")
    pointer, manifest = require_installation()
    if (
        pointer.user_data_dir != request.user_data_dir
        or pointer.installation_id != request.installation_id
        or manifest.data_generation != request.source_data_generation
        or manifest.migration_state != "clean"
    ):
        raise TargetPlanningError("planning installation identity or state changed")
    root = Path(request.user_data_dir)
    registry = build_upgrade_registry(root)
    inventory = registry.inspect(user_data_dir=root)
    invalid = next((item for item in inventory if item.state in {"corrupt", "unsupported"}), None)
    if invalid is not None:
        raise TargetPlanningError(
            sanitize_failure_summary(
                f"target cannot migrate {invalid.target.adapter_id}/{invalid.target.target_id}: "
                f"{invalid.state}, found schema {invalid.found_schema_version}, "
                f"required schema {invalid.target_schema_version}; {invalid.detail}"
            )
        )
    return PlanningResult(
        request=request,
        inventory=inventory,
        preflights=registry.preflight(user_data_dir=root),
        plan=registry.build_plan(
            source_data_generation=request.source_data_generation,
            target_data_generation=request.target_data_generation,
        ),
    )


class TargetReleasePlanner:
    """Own one temporary uv tool environment for repeated target inspection."""

    def __init__(
        self, *, release: ReleaseDescriptor, wheel: Path, constraints: Path, python: Path
    ) -> None:
        self._release = release
        self._wheel = wheel
        self._constraints = constraints
        self._python = python
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._staged_python: Path | None = None

    def __enter__(self) -> Self:
        target = self._release
        _verify_artifact(self._wheel, target.wheel)
        _verify_artifact(self._constraints, target.constraints)
        uv = _uv_executable()
        if discover_uv_version(uv) < target.minimum_uv_version:
            raise TargetPlanningError("uv is too old for the target release")
        if not self._python.is_file():
            raise TargetPlanningError("installed tool Python executable is missing")
        self._temporary = tempfile.TemporaryDirectory(prefix="ricky-target-planner-")
        root = Path(self._temporary.name)
        environment = _sanitized_environment()
        environment.update(UV_TOOL_DIR=str(root / "tools"), UV_TOOL_BIN_DIR=str(root / "bin"))
        try:
            _run(
                [
                    str(uv),
                    "tool",
                    "install",
                    "--python",
                    str(self._python),
                    "--constraints",
                    str(self._constraints),
                    "--no-config",
                    "--no-progress",
                    str(self._wheel),
                ],
                environment=environment,
                timeout=600,
            )
            self._staged_python = root / "tools" / "ricky" / "bin" / "python"
            if not self._staged_python.is_file():
                raise TargetPlanningError("target planning environment has no Python executable")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._staged_python = None

    def inspect(self, request: PlanningRequest) -> PlanningResult:
        if self._staged_python is None:
            raise TargetPlanningError("target planning environment is not open")
        target = self._release
        if (
            request.target_software_version != target.software_version
            or request.source_data_generation not in target.supported_source_data_generations
            or request.target_data_generation != target.target_data_generation
        ):
            raise TargetPlanningError("planning request does not match verified releases")
        output = _run(
            [str(self._staged_python), "-I", "-m", "ricky.upgrades.planning"],
            environment=_sanitized_environment(),
            timeout=_PLANNING_TIMEOUT_SECONDS,
            payload=request.model_dump_json().encode(),
        )
        try:
            result = PlanningResult.model_validate_json(output)
        except ValueError as exc:
            raise TargetPlanningError(
                "target release returned an invalid planning response"
            ) from exc
        if result.request != request:
            raise TargetPlanningError("target planning response does not match its request")
        return result


def _run(
    command: list[str], *, environment: dict[str, str], timeout: int, payload: bytes | None = None
) -> bytes:
    # Files bound memory even if a broken release emits excessive output.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                env=environment,
            )
        except OSError as exc:
            raise TargetPlanningError("target planning process could not start") from exc
        try:
            process.communicate(input=payload, timeout=timeout)
        except BaseException as exc:
            process.kill()
            process.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise TargetPlanningError("target planning process timed out") from exc
            raise
        output.seek(0)
        result = output.read(MAX_PLANNING_BYTES + 1)
        if len(result) > MAX_PLANNING_BYTES:
            raise TargetPlanningError("target planning response exceeds the protocol limit")
        if process.returncode != 0:
            if payload is not None:
                try:
                    failure = PlanningFailure.model_validate_json(result)
                except ValueError:
                    pass
                else:
                    raise TargetPlanningError(sanitize_failure_summary(failure.error))
            raise TargetPlanningError("target release planning failed")
        return result


def main() -> int:
    """Private protocol entry point; diagnostics never include raw exceptions."""

    try:
        payload = sys.stdin.buffer.read(MAX_PLANNING_BYTES + 1)
        if len(payload) > MAX_PLANNING_BYTES:
            raise TargetPlanningError("planning request exceeds the protocol limit")
        request = PlanningRequest.model_validate_json(payload)
        result = inspect_target(request).model_dump_json().encode()
        if len(result) > MAX_PLANNING_BYTES:
            raise TargetPlanningError("planning response exceeds the protocol limit")
    except Exception as exc:
        message = (
            sanitize_failure_summary(str(exc))
            if isinstance(exc, TargetPlanningError)
            else "Target release could not produce a valid read-only upgrade plan."
        )
        sys.stdout.write(PlanningFailure(error=message).model_dump_json() + "\n")
        return 1
    sys.stdout.buffer.write(result + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
