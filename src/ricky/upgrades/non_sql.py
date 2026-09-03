"""Read-only adapters for installation-owned non-SQL durable formats."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from ricky.config import load_settings_at
from ricky.executions.contracts import ExecutionContract
from ricky.memory.markdown import parse_note
from ricky.profiles import ProfileName, validate_profile_name
from ricky.schedules.types import ScheduleFile
from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    AdapterTarget,
    MigrationStep,
)
from ricky.workflows.run import WorkflowRun

_FORMAT_VERSION = 1
_Validator = Callable[[Path], None]


class ReadOnlyFormatAdapter:
    """Common current-format inspection for owner-defined file validators."""

    def __init__(
        self,
        *,
        adapter_id: str,
        targets: Sequence[AdapterTarget],
        validators: dict[str, _Validator],
        mutable: bool,
    ) -> None:
        self._adapter_id = adapter_id
        self._targets = tuple(targets)
        self._validators = dict(validators)
        self._mutable = mutable

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def supported_source_schema_versions(self) -> frozenset[int]:
        return frozenset({_FORMAT_VERSION})

    @property
    def target_schema_version(self) -> int:
        return _FORMAT_VERSION

    def discover(self, *, user_data_dir: Path) -> tuple[AdapterTarget, ...]:
        root = user_data_dir.resolve()
        for target in self._targets:
            path = Path(target.path)
            if not path.is_relative_to(root):
                raise ValueError(f"{self.adapter_id} target escapes user_data_dir")
        return self._targets

    def inspect(self, target: AdapterTarget) -> AdapterInspection:
        self._require_target(target)
        path = Path(target.path)
        if path.is_symlink():
            return self._result(target, "corrupt", False, "target is a symbolic link")
        if not path.exists():
            return self._result(target, "absent", True, "optional target is absent")
        try:
            self._validators[target.target_id](path)
        except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError):
            return self._result(target, "corrupt", False, "target format validation failed")
        return self._result(target, "current", True, "target uses the current format")

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        self._require_target(inspection.target)
        path = Path(inspection.target.path)
        backup_paths = (
            (str(path),)
            if self._mutable and inspection.state in {"current", "migration_required"}
            else ()
        )
        return AdapterPreflight(
            target=inspection.target,
            estimated_backup_bytes=_tree_size(path) if backup_paths else 0,
            backup_paths=backup_paths,
            touches_authored_files=self._mutable,
        )

    def plan_steps(
        self,
        *,
        source_data_generation: int,
        target_data_generation: int,
    ) -> tuple[MigrationStep, ...]:
        if source_data_generation != target_data_generation:
            raise ValueError(f"{self.adapter_id} has no declared generation migration")
        return ()

    def apply(self, step: MigrationStep) -> None:
        raise ValueError(f"{self.adapter_id} has no migration step in this data generation")

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        return self.inspect(target)

    def _require_target(self, target: AdapterTarget) -> None:
        if target.adapter_id != self.adapter_id or target.target_id not in self._validators:
            raise ValueError(f"target is not owned by {self.adapter_id}")

    def _result(
        self,
        target: AdapterTarget,
        state: str,
        integrity_valid: bool,
        detail: str,
    ) -> AdapterInspection:
        return AdapterInspection(
            target=target,
            state=state,  # type: ignore[arg-type]
            found_schema_version=_FORMAT_VERSION if state == "current" else None,
            target_schema_version=_FORMAT_VERSION,
            integrity_valid=integrity_valid,
            detail=detail,
        )


class ConfigurationUpgradeAdapter(ReadOnlyFormatAdapter):
    """Configuration-owned raw TOML inspection before current settings construction."""

    def __init__(self, *, user_data_dir: Path, profile_roots: Sequence[Path]) -> None:
        root = user_data_dir.resolve()
        paths = [("installation", root / "ricky.toml")]
        paths.extend(
            (f"profile-{profile.name}", profile / "ricky.toml") for profile in sorted(profile_roots)
        )
        targets = tuple(_file_target("configuration", target_id, path) for target_id, path in paths)
        validators: dict[str, _Validator] = {target.target_id: _validate_toml for target in targets}
        # The installation config is also validated through the complete typed
        # current settings boundary. Disabled-profile documents remain raw-TOML
        # validated because ordinary settings intentionally do not load them.
        validators["installation"] = lambda path: _validate_installation_config(path, root)
        super().__init__(
            adapter_id="configuration",
            targets=targets,
            validators=validators,
            mutable=True,
        )


class SchedulesUpgradeAdapter(ReadOnlyFormatAdapter):
    """Schedule-owned strict schedules.toml inspection."""

    def __init__(self, path: Path) -> None:
        target = _file_target("schedules", "desired-state", path)
        super().__init__(
            adapter_id="schedules",
            targets=(target,),
            validators={target.target_id: _validate_schedules},
            mutable=True,
        )


class WorkflowRunsUpgradeAdapter(ReadOnlyFormatAdapter):
    """Workflow-owned checkpoint-tree inspection without rewriting history."""

    def __init__(self, root: Path) -> None:
        target = _tree_target("workflow_runs", "checkpoints", root)
        super().__init__(
            adapter_id="workflow_runs",
            targets=(target,),
            validators={target.target_id: _validate_workflow_runs},
            mutable=False,
        )


class ExecutionContractsUpgradeAdapter(ReadOnlyFormatAdapter):
    """Execution-owned immutable contract-snapshot inspection."""

    def __init__(self, root: Path) -> None:
        target = _tree_target("execution_contracts", "snapshots", root)
        super().__init__(
            adapter_id="execution_contracts",
            targets=(target,),
            validators={target.target_id: _validate_execution_contracts},
            mutable=False,
        )


class JobGeneratedStateUpgradeAdapter(ReadOnlyFormatAdapter):
    """Jobs-owned generated JSON payload inspection beside its SQLite ledger."""

    def __init__(self, root: Path) -> None:
        target = _tree_target("job_generated_state", "generated-state", root)
        super().__init__(
            adapter_id="job_generated_state",
            targets=(target,),
            validators={target.target_id: _validate_json_tree},
            mutable=True,
        )


class MemoryUpgradeAdapter(ReadOnlyFormatAdapter):
    """Memory-owned profile note and generated-index inspection."""

    def __init__(self, profile_roots: Sequence[Path]) -> None:
        targets: list[AdapterTarget] = []
        validators: dict[str, _Validator] = {}
        for profile_root in sorted(profile_roots):
            profile = validate_profile_name(profile_root.name)
            target_id = f"profile-{profile}"
            target = _tree_target("memory", target_id, profile_root / "memory")
            targets.append(target)
            validators[target_id] = lambda path, owner=profile: _validate_memory(path, owner)
        super().__init__(
            adapter_id="memory",
            targets=targets,
            validators=validators,
            mutable=True,
        )


def _file_target(adapter_id: str, target_id: str, path: Path) -> AdapterTarget:
    canonical = path.resolve()
    return AdapterTarget(
        adapter_id=adapter_id,
        target_id=target_id,
        path=str(canonical),
        physical_path=str(canonical),
        kind="file",
    )


def _tree_target(adapter_id: str, target_id: str, path: Path) -> AdapterTarget:
    canonical = path.resolve()
    return AdapterTarget(
        adapter_id=adapter_id,
        target_id=target_id,
        path=str(canonical),
        physical_path=str(canonical),
        kind="tree",
    )


def _validate_toml(path: Path) -> None:
    with path.open("rb") as stream:
        tomllib.load(stream)


def _validate_installation_config(path: Path, root: Path) -> None:
    _validate_toml(path)
    load_settings_at(root)


def _validate_schedules(path: Path) -> None:
    with path.open("rb") as stream:
        ScheduleFile.model_validate(tomllib.load(stream))


def _validate_workflow_runs(path: Path) -> None:
    _require_real_directory(path)
    for item in sorted(path.glob("workflow_*.json")):
        if item.is_symlink() or not item.is_file():
            raise ValueError("workflow checkpoint must be a real file")
        run = WorkflowRun.model_validate_json(item.read_text(encoding="utf-8"))
        if item.stem != run.id:
            raise ValueError("workflow checkpoint identity mismatch")


def _validate_execution_contracts(path: Path) -> None:
    _require_real_directory(path)
    for item in sorted(path.glob("*/contract.json")):
        if item.is_symlink() or not item.is_file():
            raise ValueError("execution contract must be a real file")
        contract = ExecutionContract.model_validate_json(item.read_text(encoding="utf-8"))
        if item.parent.name != contract.digest:
            raise ValueError("execution contract digest path mismatch")
    for item in sorted(path.glob("*/skills/*/skill.json")):
        _validate_json_file(item)


def _validate_json_tree(path: Path) -> None:
    _require_real_directory(path)
    for item in sorted(path.rglob("*.json")):
        _validate_json_file(item)


def _validate_json_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("durable JSON target must be a real file")
    json.loads(path.read_text(encoding="utf-8"))


def _validate_memory(path: Path, profile: ProfileName) -> None:
    _require_real_directory(path)
    for item in sorted(path.glob("*.md")):
        if item.is_symlink() or not item.is_file():
            raise ValueError("memory target must be a real file")
        if item.name == "INDEX.md":
            item.read_text(encoding="utf-8")
        else:
            note = parse_note(item.read_text(encoding="utf-8"), profile=profile)
            if item.stem != note.slug:
                raise ValueError("memory note filename does not match its slug")


def _require_real_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("durable target must be a real directory")


def _tree_size(path: Path) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValueError("backup target tree cannot contain symbolic links")
        if item.is_file():
            total += item.stat().st_size
    return total


def confined_profile_roots(user_data_dir: Path) -> tuple[Path, ...]:
    """Discover every valid profile directory, including disabled profiles, without writes."""

    profiles = user_data_dir.resolve() / "profiles"
    if not profiles.exists():
        return ()
    if profiles.is_symlink() or not profiles.is_dir():
        raise ValueError("profiles root must be a real directory")
    roots: list[Path] = []
    for candidate in sorted(profiles.iterdir()):
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("profile roots must be real directories")
        validate_profile_name(candidate.name)
        roots.append(candidate.resolve())
    return tuple(roots)
