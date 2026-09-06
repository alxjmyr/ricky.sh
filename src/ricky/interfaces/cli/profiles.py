"""Profile lifecycle CLI commands."""

from __future__ import annotations

import asyncio

import typer

from ricky.installation import InstallationError
from ricky.interfaces.cli.render import CliRenderer
from ricky.interfaces.cli.results import emit_result, fail
from ricky.profiles.management import (
    ProfileManagementError,
    add_profile,
    check_profile_deletion,
    delete_profile,
    set_default_profile,
)


def register_profile_commands(root: typer.Typer) -> None:
    """Attach profile lifecycle commands to the main CLI."""

    profile_app = typer.Typer(
        help="Manage private profile compartments and the default profile.",
        no_args_is_help=True,
        add_completion=False,
    )
    root.add_typer(profile_app, name="profile")

    @profile_app.command("set-default")
    def profile_set_default(
        name: str = typer.Argument(..., help="Existing enabled profile name."),
        as_json: bool = typer.Option(False, "--json", help="Emit a machine-readable result."),
    ) -> None:
        """Use an existing profile as the default for future commands."""

        renderer = CliRenderer()
        try:
            result = set_default_profile(name)
        except (InstallationError, OSError, ProfileManagementError, ValueError) as exc:
            fail(exc, renderer, as_json=as_json, prefix="Profile error")
        emit_result(
            result,
            renderer,
            as_json=as_json,
            human=(
                f"Default profile set to {result.default_profile}."
                if result.changed
                else f"Default profile is already {result.default_profile}."
            ),
        )

    @profile_app.command("add")
    def profile_add(
        name: str = typer.Argument(..., help="Canonical lowercase profile name."),
        as_json: bool = typer.Option(False, "--json", help="Emit a machine-readable result."),
    ) -> None:
        """Create and enable one minimal private profile scaffold."""

        renderer = CliRenderer()
        try:
            result = add_profile(name)
        except (InstallationError, OSError, ProfileManagementError, ValueError) as exc:
            fail(exc, renderer, as_json=as_json, prefix="Profile error")
        emit_result(
            result,
            renderer,
            as_json=as_json,
            human=(
                f"Created profile {result.profile} at {result.profile_dir}.\n"
                f"Configuration: {result.config_path}\n"
                f"Default profile remains {result.default_profile}."
            ),
        )

    @profile_app.command("delete")
    def profile_delete(
        name: str = typer.Argument(..., help="Existing profile name."),
        new_default: str | None = typer.Option(
            None,
            "--new-default",
            help="Replacement required when deleting the current default.",
        ),
        yes: bool = typer.Option(False, "--yes", help="Skip the destructive confirmation."),
        as_json: bool = typer.Option(False, "--json", help="Emit a machine-readable result."),
    ) -> None:
        """Permanently delete one unreferenced profile and its owned data."""

        renderer = CliRenderer()
        if as_json and not yes:
            # A confirmation prompt and a cancellation notice would both land in
            # the stream that must carry exactly one machine-readable result.
            fail(
                ProfileManagementError("--json profile deletion requires --yes"),
                renderer,
                as_json=True,
                prefix="Profile error",
            )
        if not renderer.input_session.interactive and not yes:
            fail(
                ProfileManagementError("unattended profile deletion requires --yes"),
                renderer,
                as_json=as_json,
                prefix="Profile error",
            )
        try:
            # Refuse a missing, reserved, default, or still-referenced profile
            # before asking the user to confirm an irreversible deletion. The
            # authoritative recheck runs inside the exclusive operation lock.
            plan = asyncio.run(check_profile_deletion(name, new_default=new_default))
        except (InstallationError, OSError, ProfileManagementError, ValueError) as exc:
            fail(exc, renderer, as_json=as_json, prefix="Profile error")
        if not yes and not typer.confirm(
            f"Permanently delete profile {plan.profile} and all data at\n  {plan.profile_dir}?",
            default=False,
        ):
            renderer.render_status("Profile deletion cancelled.", style="yellow")
            return
        try:
            result = asyncio.run(delete_profile(name, new_default=new_default))
        except (InstallationError, OSError, ProfileManagementError, ValueError) as exc:
            fail(exc, renderer, as_json=as_json, prefix="Profile error")
        emit_result(
            result,
            renderer,
            as_json=as_json,
            human=(
                f"Deleted profile {result.profile} and its data at {result.profile_dir}.\n"
                f"Default profile: {result.default_profile}."
            ),
        )
