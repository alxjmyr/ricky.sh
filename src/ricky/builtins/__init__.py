"""Resources distributed with Ricky and discovered in every installation.

Bundled skills, workflows, and jobs are product surface. They ship inside the
wheel, resolve identically in a development tree and an installed environment,
and never depend on the working directory or a project checkout.

Ricky never writes below this root. The authoring skills create bundles below
the primary profile's user data root.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "BUNDLED_JOBS_DIR",
    "BUNDLED_SKILLS_DIR",
    "BUNDLED_WORKFLOWS_DIR",
    "bundled_jobs_dir",
    "bundled_root",
    "bundled_skills_dir",
    "bundled_workflows_dir",
]

BUNDLED_SKILLS_DIR = "skills"
BUNDLED_WORKFLOWS_DIR = "workflows"
BUNDLED_JOBS_DIR = "jobs"


def bundled_root() -> Path:
    """Return the read-only root holding resources shipped with Ricky.

    The package directory is the root in both a development tree and an
    installed wheel, so discovery needs no separate installed-layout branch.
    """

    return Path(__file__).resolve().parent


def bundled_skills_dir() -> Path:
    """Return the read-only root holding skills shipped with Ricky."""

    return bundled_root() / BUNDLED_SKILLS_DIR


def bundled_workflows_dir() -> Path:
    """Return the read-only root holding workflows shipped with Ricky."""

    return bundled_root() / BUNDLED_WORKFLOWS_DIR


def bundled_jobs_dir() -> Path:
    """Return the read-only root holding jobs shipped with Ricky."""

    return bundled_root() / BUNDLED_JOBS_DIR
