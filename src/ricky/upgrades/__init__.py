"""Released-installation upgrade contracts and read-only planning."""

from ricky.upgrades.environment import (
    InstalledToolEnvironment,
    UpgradeEnvironmentError,
    discover_installed_tool_environment,
)
from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    AdapterTarget,
    CompatibilityResult,
    MigrationPlan,
    MigrationStep,
    ReleaseArtifact,
    ReleaseDescriptor,
    UpgradeCheckRequest,
    UpgradeCheckResult,
)
from ricky.upgrades.registry import (
    MigrationRegistry,
    UpgradeAdapter,
    UpgradeRegistry,
    plan_upgrade,
)
from ricky.upgrades.releases import (
    GitHubReleaseResolver,
    LocalReleaseResolver,
    ReleaseResolutionError,
    cache_release_artifacts,
)
from ricky.upgrades.service import ReleaseResolver, UpgradeCheckService, check_upgrade
from ricky.upgrades.versions import (
    CURRENT_DATA_GENERATION,
    SUPPORTED_DATA_GENERATIONS,
    ReleaseVersion,
    require_installed_release_version,
)

__all__ = [
    "CURRENT_DATA_GENERATION",
    "SUPPORTED_DATA_GENERATIONS",
    "AdapterInspection",
    "AdapterPreflight",
    "AdapterTarget",
    "CompatibilityResult",
    "InstalledToolEnvironment",
    "GitHubReleaseResolver",
    "LocalReleaseResolver",
    "MigrationPlan",
    "MigrationRegistry",
    "MigrationStep",
    "ReleaseArtifact",
    "ReleaseDescriptor",
    "ReleaseResolver",
    "ReleaseResolutionError",
    "ReleaseVersion",
    "UpgradeAdapter",
    "UpgradeCheckRequest",
    "UpgradeCheckResult",
    "UpgradeCheckService",
    "UpgradeEnvironmentError",
    "UpgradeRegistry",
    "check_upgrade",
    "cache_release_artifacts",
    "discover_installed_tool_environment",
    "plan_upgrade",
    "require_installed_release_version",
]
