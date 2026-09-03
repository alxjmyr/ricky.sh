"""Pure read-only upgrade checking over an injected release resolver."""

from __future__ import annotations

import asyncio
import shutil
import sys
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from ricky import __version__
from ricky.installation import require_installation
from ricky.upgrades.models import (
    CompatibilityResult,
    ReleaseDescriptor,
    UpgradeCheckRequest,
    UpgradeCheckResult,
)
from ricky.upgrades.registry import UpgradeRegistry
from ricky.upgrades.versions import (
    CURRENT_DATA_GENERATION,
    SUPPORTED_DATA_GENERATIONS,
    ReleaseVersion,
)


class ReleaseResolver(Protocol):
    """Optional network or test boundary for selecting one stable release."""

    async def resolve(
        self, requested_version: ReleaseVersion | None
    ) -> ReleaseDescriptor | None: ...


class UpgradeCheckService:
    """Report release and generation compatibility without mutating installation state."""

    def __init__(
        self,
        *,
        resolver: ReleaseResolver | None = None,
        registry: UpgradeRegistry | None = None,
    ) -> None:
        self._resolver = resolver
        self._registry = registry or UpgradeRegistry.current()

    async def check(self, request: UpgradeCheckRequest) -> UpgradeCheckResult:
        """Check an optional release source; a missing source performs no I/O."""

        local_issues = _local_compatibility_issues(request)
        if self._resolver is None:
            compatibility = CompatibilityResult(
                compatible=not local_issues,
                issues=tuple(local_issues),
            )
            return UpgradeCheckResult(
                status="incompatible" if local_issues else "no_update",
                current_software_version=request.current_software_version,
                current_data_generation=request.current_data_generation,
                compatibility=compatibility,
                inventory=request.inventory,
                detail=(
                    "; ".join(local_issues)
                    if local_issues
                    else "no update available because no release source is configured"
                ),
            )

        selected = await self._resolver.resolve(request.requested_version)
        if selected is None:
            compatibility = CompatibilityResult(
                compatible=not local_issues,
                issues=tuple(local_issues),
            )
            return UpgradeCheckResult(
                status="incompatible" if local_issues else "no_update",
                current_software_version=request.current_software_version,
                current_data_generation=request.current_data_generation,
                compatibility=compatibility,
                inventory=request.inventory,
                detail="; ".join(local_issues) if local_issues else "no update available",
            )

        issues = [*local_issues, *_release_compatibility_issues(request, selected)]
        update_available = selected.software_version > request.current_software_version
        migration_required = selected.target_data_generation != request.current_data_generation
        reconciliation_required = update_available
        plan = None
        if not issues:
            try:
                plan = self._registry.build_plan(
                    source_data_generation=request.current_data_generation,
                    target_data_generation=selected.target_data_generation,
                )
            except ValueError as exc:
                issues.append(str(exc))

        compatibility = CompatibilityResult(
            compatible=not issues,
            issues=tuple(issues),
            migration_required=migration_required,
            managed_reconciliation_required=reconciliation_required,
        )
        if issues:
            status = "incompatible"
            detail = "; ".join(issues)
        elif update_available:
            status = "update_available"
            detail = f"Ricky {selected.software_version} is available"
        else:
            status = "no_update"
            detail = "the installed Ricky release is current"
        return UpgradeCheckResult(
            status=status,
            current_software_version=request.current_software_version,
            current_data_generation=request.current_data_generation,
            selected_release=selected,
            compatibility=compatibility,
            plan=plan,
            inventory=request.inventory,
            detail=detail,
        )


async def check_upgrade(
    descriptor_source: ReleaseResolver | None = None,
    requested_version: str | ReleaseVersion | None = None,
) -> UpgradeCheckResult:
    """Check the initialized installation through the optional release source.

    With no source this reads only the bootstrap pointer and installation
    manifest. In particular, it neither invokes uv nor takes the operation lock,
    so a local current-state check creates no lifecycle files.
    """

    # Import the whole-installation composition lazily. Owner adapters depend on
    # the model submodule, and importing them while the package facade itself is
    # initializing would otherwise create an adapter/package cycle.
    from ricky.upgrades.inventory import (
        build_upgrade_registry,
        inspect_upgrade_inventory,
    )

    pointer, manifest = require_installation()
    user_data_dir = Path(pointer.user_data_dir)
    inventory = inspect_upgrade_inventory(user_data_dir)
    registry = (
        UpgradeRegistry.current()
        if any(item.state in {"corrupt", "unsupported"} for item in inventory)
        else build_upgrade_registry(user_data_dir)
    )
    requested = (
        None
        if requested_version is None
        else (
            requested_version
            if isinstance(requested_version, ReleaseVersion)
            else ReleaseVersion.parse(requested_version)
        )
    )
    uv_version = None if descriptor_source is None else await _discover_uv_version()
    request = UpgradeCheckRequest(
        current_software_version=ReleaseVersion.parse(__version__),
        current_data_generation=getattr(
            manifest,
            "data_generation",
            CURRENT_DATA_GENERATION,
        ),
        requested_version=requested,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        uv_version=uv_version,
        inventory=inventory,
    )
    return await UpgradeCheckService(
        resolver=descriptor_source,
        registry=registry,
    ).check(request)


async def _discover_uv_version() -> ReleaseVersion | None:
    executable = shutil.which("uv")
    if executable is None:
        return None
    process = await asyncio.create_subprocess_exec(
        executable,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        return None
    except asyncio.CancelledError:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        await process.communicate()
        raise
    if process.returncode != 0:
        return None
    fields = stdout.decode("utf-8", errors="replace").strip().split()
    if len(fields) < 2 or fields[0] != "uv":
        return None
    try:
        return ReleaseVersion.parse(fields[1])
    except ValueError:
        return None


def _local_compatibility_issues(request: UpgradeCheckRequest) -> list[str]:
    issues: list[str] = []
    if request.current_data_generation not in SUPPORTED_DATA_GENERATIONS:
        issues.append(
            f"data generation {request.current_data_generation} is not supported by this release"
        )
    for inspection in request.inventory:
        if inspection.state in {"corrupt", "unsupported"}:
            issues.append(
                f"{inspection.target.adapter_id} target {inspection.target.path}: "
                f"{inspection.detail}"
            )
    return issues


def _release_compatibility_issues(
    request: UpgradeCheckRequest,
    release: ReleaseDescriptor,
) -> list[str]:
    issues: list[str] = []
    if (
        request.requested_version is not None
        and release.software_version != request.requested_version
    ):
        issues.append("selected release does not match the exact requested version")
    if release.software_version < request.current_software_version:
        issues.append("release is older than the installed Ricky version")
    if request.current_data_generation not in release.supported_source_data_generations:
        issues.append(
            f"release does not support source data generation {request.current_data_generation}"
        )
    if release.target_data_generation not in SUPPORTED_DATA_GENERATIONS:
        issues.append(
            f"this Ricky build cannot migrate to data generation {release.target_data_generation}"
        )
    if request.python_parts < release.minimum_python_parts:
        issues.append(
            f"release requires Python {release.python_requirement}; found {request.python_version}"
        )
    if request.uv_version is None:
        issues.append("uv version is unavailable")
    elif request.uv_version < release.minimum_uv_version:
        issues.append(
            f"release requires uv {release.minimum_uv_version} or newer; found {request.uv_version}"
        )
    return issues
