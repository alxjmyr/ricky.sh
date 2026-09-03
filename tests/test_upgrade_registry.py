"""Coordinator ordering, grouping, and owner-dispatch coverage."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    AdapterTarget,
    MigrationStep,
)
from ricky.upgrades.registry import UpgradeRegistry


@dataclass
class _Adapter:
    adapter_id: str
    targets: tuple[AdapterTarget, ...]
    steps: tuple[MigrationStep, ...] = ()
    applied: list[str] = field(default_factory=list)

    @property
    def supported_source_schema_versions(self) -> frozenset[int]:
        return frozenset({1})

    @property
    def target_schema_version(self) -> int:
        return 1

    def discover(self, *, user_data_dir: Path) -> tuple[AdapterTarget, ...]:
        assert all(Path(target.path).is_relative_to(user_data_dir) for target in self.targets)
        return self.targets

    def inspect(self, target: AdapterTarget) -> AdapterInspection:
        return AdapterInspection(
            target=target,
            state="current",
            found_schema_version=1,
            target_schema_version=1,
            integrity_valid=True,
            detail="current test target",
        )

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        return AdapterPreflight(target=inspection.target, estimated_backup_bytes=0)

    def plan_steps(
        self,
        *,
        source_data_generation: int,
        target_data_generation: int,
    ) -> tuple[MigrationStep, ...]:
        assert source_data_generation == target_data_generation == 1
        return self.steps

    def apply(self, step: MigrationStep) -> None:
        self.applied.append(step.step_id)

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        return self.inspect(target)


def _target(root: Path, adapter_id: str, name: str, physical: Path) -> AdapterTarget:
    return AdapterTarget(
        adapter_id=adapter_id,
        target_id=name,
        path=str((root / f"{name}.sqlite3").resolve()),
        physical_path=str(physical.resolve()),
        kind="sqlite",
    )


def _step(
    adapter_id: str,
    step_id: str,
    target: AdapterTarget,
    *,
    depends_on: tuple[str, ...] = (),
) -> MigrationStep:
    return MigrationStep(
        adapter_id=adapter_id,
        step_id=step_id,
        target_id=target.target_id,
        physical_path=target.physical_path,
        source_schema_version=0,
        target_schema_version=1,
        depends_on=depends_on,
    )


def test_plan_topologically_orders_steps_with_deterministic_physical_ties(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    later_target = _target(root, "later", "later", root / "z.sqlite3")
    first_target = _target(root, "first", "first", root / "a.sqlite3")
    dependent_target = _target(root, "dependent", "dependent", root / "b.sqlite3")
    first = _step("first", "prepare", first_target)
    dependent = _step(
        "dependent",
        "finish",
        dependent_target,
        depends_on=("first:prepare",),
    )
    later = _step("later", "independent", later_target)
    registry = UpgradeRegistry(
        (
            _Adapter("later", (later_target,), (later,)),
            _Adapter("dependent", (dependent_target,), (dependent,)),
            _Adapter("first", (first_target,), (first,)),
        )
    )

    plan = registry.build_plan(source_data_generation=1, target_data_generation=1)

    assert [(step.adapter_id, step.step_id) for step in plan.steps] == [
        ("first", "prepare"),
        ("dependent", "finish"),
        ("later", "independent"),
    ]
    assert plan == registry.build_plan(source_data_generation=1, target_data_generation=1)


@pytest.mark.parametrize(
    "dependencies",
    [
        (("missing",), ()),
        (("two:second",), ("one:first",)),
    ],
)
def test_plan_refuses_unknown_or_cyclic_dependencies(
    tmp_path: Path,
    dependencies: tuple[tuple[str, ...], tuple[str, ...]],
) -> None:
    root = tmp_path.resolve()
    one_target = _target(root, "one", "one", root / "one.sqlite3")
    two_target = _target(root, "two", "two", root / "two.sqlite3")
    registry = UpgradeRegistry(
        (
            _Adapter(
                "one",
                (one_target,),
                (_step("one", "first", one_target, depends_on=dependencies[0]),),
            ),
            _Adapter(
                "two",
                (two_target,),
                (_step("two", "second", two_target, depends_on=dependencies[1]),),
            ),
        )
    )

    with pytest.raises(ValueError, match="dependency"):
        registry.build_plan(source_data_generation=1, target_data_generation=1)


def test_preflight_groups_shared_physical_store_and_dispatches_owner(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    shared = root / "shared.sqlite3"
    alpha_target = _target(root, "alpha", "alpha", shared)
    beta_target = _target(root, "beta", "beta", shared)
    alpha_step = _step("alpha", "migrate", alpha_target)
    alpha = _Adapter("alpha", (alpha_target,), (alpha_step,))
    beta = _Adapter("beta", (beta_target,))
    registry = UpgradeRegistry((beta, alpha))

    groups = registry.physical_preflight_groups(user_data_dir=root)
    registry.apply_step(alpha_step)
    inspection = registry.verify_step(alpha_step, user_data_dir=root)

    assert len(groups) == 1
    assert [item.target.adapter_id for item in groups[0]] == ["alpha", "beta"]
    assert alpha.applied == ["migrate"]
    assert beta.applied == []
    assert inspection.target == alpha_target
