"""Adapter boundary and deterministic migration-plan registry."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    AdapterTarget,
    MigrationPlan,
    MigrationStep,
)
from ricky.upgrades.versions import SUPPORTED_DATA_GENERATIONS


class UpgradeAdapter(Protocol):
    """Complete subsystem-owned discovery, migration, and verification boundary."""

    @property
    def adapter_id(self) -> str: ...

    @property
    def supported_source_schema_versions(self) -> frozenset[int]: ...

    @property
    def target_schema_version(self) -> int: ...

    def discover(self, *, user_data_dir: Path) -> tuple[AdapterTarget, ...]: ...

    def inspect(self, target: AdapterTarget) -> AdapterInspection: ...

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight: ...

    def plan_steps(
        self,
        *,
        source_data_generation: int,
        target_data_generation: int,
    ) -> tuple[MigrationStep, ...]: ...

    def apply(self, step: MigrationStep) -> None: ...

    def verify(self, target: AdapterTarget) -> AdapterInspection: ...


class UpgradeRegistry:
    """Ordered adapter registry for whole-installation migration planning."""

    def __init__(self, adapters: Sequence[UpgradeAdapter] = ()) -> None:
        ordered = tuple(sorted(adapters, key=lambda adapter: adapter.adapter_id))
        identities = [adapter.adapter_id for adapter in ordered]
        if len(identities) != len(set(identities)):
            raise ValueError("upgrade registry contains a duplicate adapter id")
        self._adapters = ordered

    @property
    def adapters(self) -> tuple[UpgradeAdapter, ...]:
        return self._adapters

    @classmethod
    def current(cls) -> UpgradeRegistry:
        """Return the Phase 1 registry, which has no migration adapters."""

        return cls()

    def build_plan(
        self,
        *,
        source_data_generation: int,
        target_data_generation: int,
    ) -> MigrationPlan:
        """Build a deterministic plan without discovering or writing durable state."""

        supported = set(SUPPORTED_DATA_GENERATIONS)
        if source_data_generation not in supported or target_data_generation not in supported:
            raise ValueError("upgrade registry does not support the requested data generation")
        discovered_steps = tuple(
            step
            for adapter in self._adapters
            for step in adapter.plan_steps(
                source_data_generation=source_data_generation,
                target_data_generation=target_data_generation,
            )
        )
        adapter_ids = {adapter.adapter_id for adapter in self._adapters}
        mismatched = sorted({step.adapter_id for step in discovered_steps} - adapter_ids)
        if mismatched:
            raise ValueError(
                "upgrade adapter returned a step owned by another adapter: " + ", ".join(mismatched)
            )
        if source_data_generation != target_data_generation and not discovered_steps:
            raise ValueError("no migration path exists for the requested data-generation change")
        steps = _topological_steps(discovered_steps)
        return MigrationPlan.create(
            source_data_generation=source_data_generation,
            target_data_generation=target_data_generation,
            steps=steps,
        )

    def inspect(self, *, user_data_dir: Path) -> tuple[AdapterInspection, ...]:
        """Discover and inspect every adapter target without creating durable state."""

        root = user_data_dir.expanduser().resolve()
        inspections: list[AdapterInspection] = []
        identities: set[tuple[str, str]] = set()
        for adapter in self._adapters:
            targets = sorted(
                adapter.discover(user_data_dir=root),
                key=lambda target: (target.physical_path, target.target_id),
            )
            for target in targets:
                if target.adapter_id != adapter.adapter_id:
                    raise ValueError("upgrade target is owned by the wrong adapter")
                identity = (target.adapter_id, target.target_id)
                if identity in identities:
                    raise ValueError("upgrade inventory contains a duplicate target identity")
                identities.add(identity)
                inspection = adapter.inspect(target)
                if inspection.target != target:
                    raise ValueError("upgrade inspection changed its target identity")
                inspections.append(inspection)
        return tuple(
            sorted(
                inspections,
                key=lambda item: (
                    item.target.physical_path,
                    item.target.adapter_id,
                    item.target.target_id,
                ),
            )
        )

    def preflight(self, *, user_data_dir: Path) -> tuple[AdapterPreflight, ...]:
        """Declare every adapter-owned backup and mutation target without writes."""

        adapters = {adapter.adapter_id: adapter for adapter in self._adapters}
        results: list[AdapterPreflight] = []
        for inspection in self.inspect(user_data_dir=user_data_dir):
            adapter = adapters[inspection.target.adapter_id]
            result = adapter.preflight(inspection)
            if result.target != inspection.target:
                raise ValueError("upgrade preflight changed its target identity")
            results.append(result)
        return tuple(
            sorted(
                results,
                key=lambda item: (
                    item.target.physical_path,
                    item.target.adapter_id,
                    item.target.target_id,
                ),
            )
        )

    def physical_preflight_groups(
        self,
        *,
        user_data_dir: Path,
    ) -> tuple[tuple[AdapterPreflight, ...], ...]:
        """Group co-located owner contracts into deterministic physical plans."""

        grouped: list[list[AdapterPreflight]] = []
        for item in self.preflight(user_data_dir=user_data_dir):
            if not grouped or (grouped[-1][0].target.physical_path != item.target.physical_path):
                grouped.append([item])
            else:
                grouped[-1].append(item)
        return tuple(tuple(group) for group in grouped)

    def apply_step(self, step: MigrationStep) -> None:
        """Dispatch one journaled step only to its declared subsystem owner."""

        adapter = self._adapter(step.adapter_id)
        adapter.apply(step)

    def verify_step(
        self,
        step: MigrationStep,
        *,
        user_data_dir: Path,
    ) -> AdapterInspection:
        """Verify the exact target bound to an applied migration step."""

        adapter, target = self._step_owner_target(step, user_data_dir=user_data_dir)
        inspection = adapter.verify(target)
        if inspection.target != target:
            raise ValueError("upgrade verification changed its target identity")
        return inspection

    def inspect_step(
        self,
        step: MigrationStep,
        *,
        user_data_dir: Path,
    ) -> AdapterInspection:
        """Inspect an original journaled target to decide apply versus verify."""

        adapter, target = self._step_owner_target(step, user_data_dir=user_data_dir)
        inspection = adapter.inspect(target)
        if inspection.target != target:
            raise ValueError("upgrade inspection changed its target identity")
        return inspection

    def _adapter(self, adapter_id: str) -> UpgradeAdapter:
        for adapter in self._adapters:
            if adapter.adapter_id == adapter_id:
                return adapter
        raise ValueError(f"unknown upgrade adapter: {adapter_id}")

    def _step_owner_target(
        self,
        step: MigrationStep,
        *,
        user_data_dir: Path,
    ) -> tuple[UpgradeAdapter, AdapterTarget]:
        adapter = self._adapter(step.adapter_id)
        targets = {
            target.target_id: target
            for target in adapter.discover(user_data_dir=user_data_dir.expanduser().resolve())
        }
        target = targets.get(step.target_id)
        if target is None or step.physical_path != target.physical_path:
            raise ValueError("migration step target does not match current discovery")
        return adapter, target


# Public domain language uses "migration registry" for the adapter collection.
# Keep the original implementation name as a compatibility spelling while the
# Phase 1 CLI and tests adopt the precise name.
MigrationRegistry = UpgradeRegistry


def plan_upgrade(
    registry: UpgradeRegistry,
    source_generation: int,
    target_generation: int,
) -> MigrationPlan:
    """Build one read-only deterministic plan through the supplied registry."""

    return registry.build_plan(
        source_data_generation=source_generation,
        target_data_generation=target_generation,
    )


def _topological_steps(steps: tuple[MigrationStep, ...]) -> tuple[MigrationStep, ...]:
    """Order declared dependencies with deterministic physical-path tie breaking."""

    by_identity = {(step.adapter_id, step.step_id): step for step in steps}
    if len(by_identity) != len(steps):
        raise ValueError("migration plan contains a duplicate step identity")
    aliases: dict[str, tuple[str, str]] = {}
    ambiguous: set[str] = set()
    for identity in by_identity:
        adapter_id, step_id = identity
        for alias in (step_id, f"{adapter_id}:{step_id}"):
            if alias in aliases and aliases[alias] != identity:
                ambiguous.add(alias)
            else:
                aliases[alias] = identity
    for alias in ambiguous:
        aliases.pop(alias, None)

    dependencies: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for identity, step in by_identity.items():
        resolved: set[tuple[str, str]] = set()
        for dependency in step.depends_on:
            target = aliases.get(dependency)
            if target is None:
                raise ValueError(
                    f"migration step {step.adapter_id}:{step.step_id} has an unknown "
                    f"or ambiguous dependency: {dependency}"
                )
            if target == identity:
                raise ValueError("migration step cannot depend on itself")
            resolved.add(target)
        dependencies[identity] = resolved

    ordered: list[MigrationStep] = []
    remaining = dict(dependencies)
    while remaining:
        ready = [identity for identity, required in remaining.items() if not required]
        if not ready:
            raise ValueError("migration plan contains a dependency cycle")
        ready.sort(key=lambda identity: _step_sort_key(by_identity[identity]))
        selected = ready[0]
        ordered.append(by_identity[selected])
        del remaining[selected]
        for required in remaining.values():
            required.discard(selected)
    return tuple(ordered)


def _step_sort_key(step: MigrationStep) -> tuple[str, str, str]:
    return (step.physical_path or "", step.adapter_id, step.step_id)
