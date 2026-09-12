"""Cross-suite safety fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def plain_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI text assertions independent of CI's forced terminal styling."""

    from typer import rich_utils

    # Rich also checks these at console creation time. Individual rendering
    # tests can opt back into a terminal explicitly.
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("TTY_COMPATIBLE", raising=False)
    # Typer reads GITHUB_ACTIONS/FORCE_COLOR at import time; changing the
    # environment in a fixture is too late. CliRunner captures plain text.
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", False)


@pytest.fixture(autouse=True)
def isolate_user_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test away from real user data and bootstrap configuration."""

    home = tmp_path.parent / f".{tmp_path.name}-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(tmp_path / "user-data"))


@pytest.fixture(autouse=True)
def refuse_real_crontab(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly rather than edit the developer's own user crontab.

    ``UserCrontabBackend`` defaults to the ``crontab`` program, which acts on
    the POSIX user's crontab no matter where ``HOME``, ``XDG_CONFIG_HOME``, or
    the data root are redirected.  A test that reaches it therefore destroys
    real installed schedules while every other isolation fixture still looks
    correct.  Tests must inject their own runner instead.
    """

    from collections.abc import Sequence

    from ricky.schedules import cron

    async def refuse(self: object, args: Sequence[str]) -> object:
        raise AssertionError(
            f"test tried to run the real crontab program: {list(args)}. "
            "Pass a fake runner to UserCrontabBackend instead."
        )

    monkeypatch.setattr(cron.SubprocessCommandRunner, "run", refuse)


@pytest.fixture
def bundled_root(tmp_path: Path) -> Path:
    """Return the per-test stand-in for the resources distributed with Ricky."""

    # Kept beside tmp_path, not inside it, so tests that assert on the exact
    # contents of tmp_path do not see this fixture's scaffolding.
    root = tmp_path.parent / f".{tmp_path.name}-bundled"
    for name in ("skills", "workflows", "jobs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(autouse=True)
def isolate_bundled_root(
    bundled_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Hide the real bundled skills, workflows, and jobs from ordinary tests.

    ``ricky.builtins`` resolves its root from the installed package, so every
    discovery call would otherwise load the shipped bundles and make unrelated
    assertions depend on product content. Tests that must exercise the real
    shipped resources declare the ``real_bundled_resources`` marker.
    """

    if request.node.get_closest_marker("real_bundled_resources") is not None:
        return

    from ricky import builtins

    monkeypatch.setattr(builtins, "bundled_root", lambda: bundled_root)
