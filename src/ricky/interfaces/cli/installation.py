"""Installation initialization and data-lifecycle CLI commands."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, Literal, Never
from uuid import uuid4

import typer
from pydantic import BaseModel

from ricky.config import (
    ModelSelection,
    load_settings,
    user_data_path,
    write_default_selection,
    write_profile_secret,
)
from ricky.gateway.lock import GatewayLock
from ricky.gateway.service_unit import MARKER, GatewayServiceUnit, ServiceUnitError
from ricky.installation import (
    InstallationError,
    adopt_inherited_operation_lock,
    bootstrap_config_dir,
    initialize_installation,
    installation_operation_lock,
    managed_schedules_installed,
    purge_installation_data,
    require_compatible_installation,
    require_decommissioned,
    require_installation,
)
from ricky.interfaces.cli.render import CliRenderer
from ricky.interfaces.cli.results import emit_result, fail
from ricky.interfaces.cli.select import provider_allowed
from ricky.llm.factory import ProviderEntry, auth_ready, provider_entries
from ricky.schedules.cron import CronError, UserCrontabBackend
from ricky.upgrades import (
    GitHubReleaseResolver,
    LocalReleaseResolver,
    ReleaseResolver,
    ReleaseVersion,
    UpgradeCheckResult,
    check_upgrade,
)
from ricky.upgrades.environment import (
    UpgradeEnvironmentError,
    discover_installed_tool_environment,
)
from ricky.upgrades.integrations import (
    ManagedIntegrationError,
    ManagedUpgradeController,
    load_managed_result,
    prepare_managed_upgrade,
    restart_gateway_after_upgrade,
    restore_gateway_after_aborted_prepare,
)
from ricky.upgrades.inventory import build_upgrade_registry
from ricky.upgrades.journal import UpgradeJournal
from ricky.upgrades.orchestrator import UpgradeCoordinator, UpgradeCoordinatorError
from ricky.upgrades.releases import ReleaseResolutionError
from ricky.upgrades.software import (
    SoftwareReplacementError,
    UpgradeHandoffComplete,
    UvToolSoftwareController,
    create_software_binding,
)

_USER_DATA_DIR_OPTION = typer.Option(
    None,
    "--user-data-dir",
    help="One-time absolute or home-relative private data root.",
)
_RELEASE_DESCRIPTOR_OPTION = typer.Option(
    [],
    "--release-descriptor",
    hidden=True,
    help="Use one or more local release-drill descriptors.",
)


def register_installation_commands(root: typer.Typer) -> None:
    """Attach installation lifecycle commands to the main CLI composition root."""

    data_app = typer.Typer(
        help="Inspect or irreversibly remove Ricky's private user data.",
        no_args_is_help=True,
        add_completion=False,
    )
    root.add_typer(data_app, name="data")

    @root.command("init")
    def initialize(
        user_data_dir: Path | None = _USER_DATA_DIR_OPTION,
        as_json: bool = typer.Option(False, "--json", help="Emit a machine-readable result."),
    ) -> None:
        """Create or verify Ricky's minimal private data scaffold."""

        renderer = CliRenderer()
        try:
            result = initialize_installation(user_data_dir)
        except (InstallationError, OSError, ValueError) as exc:
            _fail(exc, renderer, as_json=as_json)
        _emit(
            result,
            renderer,
            as_json=as_json,
            human=(
                ("Initialized" if result.created else "Verified")
                + f" Ricky installation at {result.user_data_dir}.\n"
                + f"installation id: {result.installation_id}\n"
                + f"bootstrap pointer: {result.bootstrap_path}"
            ),
        )

    @root.command("decommission")
    def decommission(
        as_json: bool = typer.Option(False, "--json", help="Emit a machine-readable result."),
    ) -> None:
        """Remove Ricky-owned services and schedules while preserving all user data."""

        renderer = CliRenderer()
        try:
            result = asyncio.run(_decommission())
        except (CronError, InstallationError, OSError, ServiceUnitError, ValueError) as exc:
            _fail(exc, renderer, as_json=as_json)
        _emit(
            result,
            renderer,
            as_json=as_json,
            human=_render_decommission(result),
        )

    @root.command("setup")
    def setup() -> None:
        """Interactively configure a local-only shared-profile model default."""

        renderer = CliRenderer()
        try:
            if not renderer.input_session.interactive:
                raise InstallationError("ricky setup requires an interactive terminal")
            asyncio.run(_setup(renderer))
        except EOFError:
            # A closed or interrupted prompt is a cancellation, not a crash.
            _fail(
                InstallationError("setup was cancelled before it saved anything"),
                renderer,
                as_json=False,
            )
        except (InstallationError, OSError, ValueError) as exc:
            _fail(exc, renderer, as_json=False)

    @root.command("upgrade")
    def upgrade(
        check: bool = typer.Option(False, "--check", help="Check without changing Ricky."),
        target: str | None = typer.Option(
            None,
            "--to",
            help="Exact stable MAJOR.MINOR.PATCH release.",
        ),
        yes: bool = typer.Option(False, "--yes", help="Confirm an unattended operation."),
        update_jobs: bool = typer.Option(
            False,
            "--update-jobs",
            help="Allow deterministic profile-job updates; never grants authority.",
        ),
        resume: bool = typer.Option(False, "--resume", help="Resume the journaled operation."),
        rollback: bool = typer.Option(
            False,
            "--rollback",
            help="Restore the verified pre-upgrade software and data pair.",
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit one machine-readable result."),
        release_descriptor: list[Path] = _RELEASE_DESCRIPTOR_OPTION,
    ) -> None:
        """Check, apply, resume, or roll back a released Ricky upgrade."""

        renderer = CliRenderer()
        try:
            modes = int(check) + int(resume) + int(rollback)
            if modes > 1:
                raise InstallationError("choose only one of --check, --resume, or --rollback")
            if (resume or rollback) and target is not None:
                raise InstallationError("--to is valid only for a check or a new upgrade")
            if (check or resume or rollback) and update_jobs:
                raise InstallationError("--update-jobs is valid only for a new upgrade")
            if check and yes:
                raise InstallationError("--yes is not valid with --check")
            if (resume or rollback) and release_descriptor:
                raise InstallationError(
                    "--release-descriptor is valid only for a check or a new upgrade"
                )

            if check:
                resolver = _release_resolver(release_descriptor)
                with installation_operation_lock(
                    mode="shared",
                    timeout_seconds=5.0,
                    operation="upgrade_check",
                ):
                    require_compatible_installation()
                    result = asyncio.run(check_upgrade(resolver, target))
                _emit(
                    result,
                    renderer,
                    as_json=as_json,
                    human=_render_upgrade_check(result),
                )
                return

            if resume or rollback:
                _validate_recovery_request(
                    resume=resume,
                    rollback=rollback,
                    yes=yes,
                    as_json=as_json,
                )

            if not (resume or rollback):
                interactive = renderer.input_session.interactive
                if as_json and (not yes or target is None):
                    raise InstallationError("--json upgrade requires --yes and an exact --to")
                if yes and target is None:
                    raise InstallationError("--yes requires an exact --to")
                if not interactive and (not yes or target is None):
                    raise InstallationError("unattended upgrade requires --yes and an exact --to")
                if target is not None:
                    ReleaseVersion.parse(target)

            if resume or rollback:
                journal = _run_upgrade_recovery(
                    rollback=rollback,
                    as_json=as_json,
                )
            else:
                journal = asyncio.run(
                    _run_upgrade_apply(
                        target=target,
                        yes=yes,
                        update_jobs=update_jobs,
                        as_json=as_json,
                        release_descriptor=release_descriptor,
                        renderer=renderer,
                    )
                )
            _emit(
                _upgrade_result(journal),
                renderer,
                as_json=as_json,
                human=_render_upgrade_result(journal),
            )
        except UpgradeHandoffComplete as handoff:
            raise typer.Exit(handoff.exit_code) from handoff
        except (
            InstallationError,
            ManagedIntegrationError,
            OSError,
            ReleaseResolutionError,
            SoftwareReplacementError,
            UpgradeCoordinatorError,
            UpgradeEnvironmentError,
            ValueError,
        ) as exc:
            _fail(exc, renderer, as_json=as_json)

    @root.command("_upgrade-handoff", hidden=True)
    def upgrade_handoff(
        operation_id: str = typer.Option(..., "--operation-id"),
        action: Literal["resume", "rollback"] = typer.Option(..., "--action"),
        lock_fd: int = typer.Option(..., "--lock-fd", min=0),
        as_json: bool = typer.Option(False, "--json"),
    ) -> None:
        """Continue an exact journal through an inherited exclusive lock."""

        renderer = CliRenderer()
        try:
            pointer, manifest = require_installation()
            if manifest.operation_id != operation_id:
                raise InstallationError("upgrade handoff operation identity does not match")
            environment = discover_installed_tool_environment()
            root_path = Path(pointer.user_data_dir)
            with adopt_inherited_operation_lock(
                lock_fd,
                directory=bootstrap_config_dir(),
                operation_id=operation_id,
            ) as lock:
                software = UvToolSoftwareController(
                    environment=environment,
                    lock=lock,
                    as_json=as_json,
                )
                coordinator = UpgradeCoordinator(
                    user_data_dir=root_path,
                    lock=lock,
                    registry=build_upgrade_registry(root_path),
                    software=software,
                    integrations=ManagedUpgradeController(
                        user_data_dir=root_path,
                        executable=Path(environment.executable),
                        run_async=asyncio.run,
                    ),
                )
                journal = coordinator.resume() if action == "resume" else coordinator.rollback()
            restart_gateway_after_upgrade(
                user_data_dir=root_path,
                executable=Path(environment.executable),
                operation_id=journal.operation_id,
            )
            _emit(
                _upgrade_result(journal),
                renderer,
                as_json=as_json,
                human=_render_upgrade_result(journal),
            )
        except UpgradeHandoffComplete as handoff:
            raise typer.Exit(handoff.exit_code) from handoff
        except (
            InstallationError,
            ManagedIntegrationError,
            OSError,
            SoftwareReplacementError,
            UpgradeCoordinatorError,
            UpgradeEnvironmentError,
            ValueError,
        ) as exc:
            _fail(exc, renderer, as_json=as_json)

    @data_app.command("purge")
    def data_purge(
        yes: bool = typer.Option(False, "--yes", help="Skip the interactive confirmation."),
        installation_id: str | None = typer.Option(
            None,
            "--installation-id",
            help="Exact installation id required for unattended deletion.",
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit a machine-readable result."),
    ) -> None:
        """Irreversibly delete one exact inactive Ricky user-data installation."""

        renderer = CliRenderer()
        try:
            pointer, manifest = require_installation()
            settings = load_settings()
            lock = GatewayLock(settings)
            if lock.is_active():
                owner = lock.read_owner()
                detail = f" (pid {owner.pid})" if owner is not None else ""
                raise InstallationError(
                    "the Ricky gateway is active"
                    + detail
                    + "; stop it before deleting installation data"
                )
            # This command composes the host crontab adapter once and hands it to
            # the lifecycle API. The refusal below is advisory: it reports an
            # installed launch surface before the irreversible prompt, and the
            # same guard runs authoritatively inside the purge lock.
            crontab = UserCrontabBackend(settings)
            asyncio.run(require_decommissioned(Path(pointer.user_data_dir), crontab=crontab))

            interactive = renderer.input_session.interactive
            if not interactive and not yes:
                raise InstallationError("unattended purge requires --yes")
            if not interactive and installation_id is None:
                raise InstallationError("unattended purge requires the exact --installation-id")
            if as_json and not yes:
                # A confirmation prompt and a cancellation notice would both land
                # in the stream that must carry exactly one machine-readable result.
                raise InstallationError(
                    "--json purge requires --yes and the exact --installation-id"
                )
            if yes and installation_id is None:
                raise InstallationError("--yes requires the exact --installation-id")
            if installation_id is not None and installation_id != manifest.installation_id:
                raise InstallationError("installation id confirmation does not match")
            if not yes:
                prompt = (
                    "Permanently delete all Ricky data at\n"
                    f"  {pointer.user_data_dir}\n"
                    f"installation id: {manifest.installation_id}?"
                )
                if not typer.confirm(prompt, default=False):
                    renderer.render_status("Ricky data purge cancelled.", style="yellow")
                    return

            result = asyncio.run(
                purge_installation_data(
                    expected_installation_id=installation_id or manifest.installation_id,
                    crontab=crontab,
                )
            )
        except (InstallationError, OSError, ValueError) as exc:
            _fail(exc, renderer, as_json=as_json)
        _emit(
            result,
            renderer,
            as_json=as_json,
            human=(
                f"Permanently removed Ricky installation data at {result.user_data_dir}.\n"
                f"Removed bootstrap pointer: {result.bootstrap_path}"
            ),
        )


async def _decommission() -> dict[str, Any]:
    pointer, manifest = require_installation()
    settings = load_settings()
    unit = GatewayServiceUnit(settings)
    installed = unit.installed()
    if installed is not None and not installed.startswith(MARKER):
        raise ServiceUnitError(
            f"{unit.unit_path} was not written by Ricky; no service action was taken"
        )
    # A gateway holds SH for its lifetime. Drain the owned launcher before
    # waiting for EX, then recheck identity and ownership under the lock.
    stop = unit.stop()
    disable = unit.disable()
    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=5.0,
        operation="decommission",
    ):
        locked_pointer, locked_manifest = require_installation()
        if locked_pointer != pointer or locked_manifest != manifest:
            raise InstallationError("Ricky installation changed while decommissioning")
        locked_installed = unit.installed()
        if locked_installed is not None and not locked_installed.startswith(MARKER):
            raise ServiceUnitError(
                f"{unit.unit_path} was not written by Ricky; no removal was performed"
            )
        removed = unit.uninstall() if locked_installed is not None else False
        reload_result = unit.daemon_reload()
        service: dict[str, Any] = {
            "state": "removed" if removed else "not_installed",
            "unit_path": str(unit.unit_path),
            "stop_exit": stop.returncode,
            "disable_exit": disable.returncode,
            "daemon_reload_exit": reload_result.returncode,
        }

        backend = UserCrontabBackend(settings)
        if await managed_schedules_installed(backend):
            cron = await backend.uninstall()
            schedules = {
                "state": "removed" if cron.changed else "not_installed",
                "backup_path": None if cron.backup_path is None else str(cron.backup_path),
            }
        else:
            # The crontab read, not a local state directory, is authoritative.
            schedules = {"state": "not_installed", "backup_path": None}
    return {
        "installation_id": manifest.installation_id,
        "user_data_dir": pointer.user_data_dir,
        "service": service,
        "schedules": schedules,
        "user_data_preserved": True,
        "bootstrap_pointer_preserved": True,
        "software_removal_command": "uv tool uninstall ricky",
    }


async def _setup(renderer: CliRenderer) -> None:
    """Configure shared-profile provider credentials and selection without network use."""

    require_compatible_installation()
    settings = load_settings()
    scope = settings.resolve_profile_scope("shared")
    runtime_settings = settings.resolve_profile_runtime_settings(scope)
    entries = [
        entry for entry in provider_entries() if provider_allowed(settings, scope, entry.name)
    ]
    if not entries:
        raise InstallationError("the shared profile allows no registered providers")

    current = settings.resolve_profile_selection(scope)
    renderer.render_status("Configure Ricky's shared profile (local checks only).", style="bold")
    renderer.render_status(
        "The shared profile is universal: every profile you add later inherits what you save here.",
        style="yellow",
    )
    renderer.render_status("Providers:", style="bold")
    for index, entry in enumerate(entries, start=1):
        readiness = (
            "locally ready"
            if auth_ready(entry.name, runtime_settings)
            else f"setup needed — {entry.auth_hint}"
        )
        selected = " (current default)" if entry.name == current.provider else ""
        renderer.render_status(
            f"  {index}. {entry.title} [{entry.name}] — {readiness}{selected}",
            style="",
        )
    entry = await _prompt_provider(entries, current.provider, renderer)

    credential_path: Path | None = None
    if entry.name in {"openrouter", "anthropic"}:
        secret_name: Literal["openrouter_api_key", "anthropic_api_key"] = (
            "openrouter_api_key" if entry.name == "openrouter" else "anthropic_api_key"
        )
        configured = getattr(runtime_settings, secret_name) is not None
        replace = True
        if configured:
            answer = (
                (
                    await renderer.read_line(
                        f"A {entry.title} credential is already configured. Replace it? [y/N]: "
                    )
                )
                .strip()
                .lower()
            )
            replace = answer in {"y", "yes"}
        if replace:
            value = await renderer.read_secret(f"Enter {entry.title} API key: ")
            if value is None:
                raise InstallationError("provider credential input was cancelled")
            credential_path = write_profile_secret(
                secret_name,
                value,
                profile="shared",
                root=user_data_path(settings),
            )
    elif not auth_ready(entry.name, runtime_settings):
        raise InstallationError(
            "the Claude Code executable is not available locally; install and authenticate "
            "it, or choose an API provider"
        )

    default_model = settings.resolve_profile_selection(scope, provider=entry.name).model
    model = (await renderer.read_line(f"Model id [{default_model}]: ")).strip() or default_model
    selection = ModelSelection(provider=entry.name, model=model)
    selection_path = write_default_selection(
        selection,
        root=user_data_path(settings),
        profile="shared",
    )
    if credential_path is not None:
        renderer.render_status(f"Saved the shared-profile credential to {credential_path}.")
    renderer.render_status(
        f"Saved shared-profile default: {selection.provider} · {selection.model}\n"
        f"  → {selection_path}",
        style="green",
    )
    renderer.render_status(
        "Setup performed local validation only; it did not contact the provider. "
        'Run `ricky ask "Hello"` to verify the configured provider.',
        style="yellow",
    )


async def _prompt_provider(
    entries: list[ProviderEntry], current: str, renderer: CliRenderer
) -> ProviderEntry:
    names = [entry.name for entry in entries]
    default_index = names.index(current) + 1 if current in names else 1
    while True:
        answer = (
            await renderer.read_line(
                f"Select provider [1-{len(entries)}, Enter keeps "
                f"{entries[default_index - 1].name}]: "
            )
        ).strip()
        if not answer:
            return entries[default_index - 1]
        if answer in names:
            return entries[names.index(answer)]
        if answer.isdigit() and 1 <= int(answer) <= len(entries):
            return entries[int(answer) - 1]
        renderer.render_status("Choose a listed provider number or name.", style="yellow")


def _render_decommission(result: dict[str, Any]) -> str:
    service = result["service"]
    schedules = result["schedules"]
    lines = [f"Gateway service: {service['state']}"]
    reported = [
        f"{action} exit {service[key]}"
        for action, key in (
            ("stop", "stop_exit"),
            ("disable", "disable_exit"),
            ("daemon-reload", "daemon_reload_exit"),
        )
        if service[key] != 0
    ]
    if reported:
        lines.append("systemctl reported: " + ", ".join(reported))
    lines.append(f"Managed schedules: {schedules['state']}")
    if schedules["backup_path"] is not None:
        lines.append(f"Crontab backup: {schedules['backup_path']}")
    lines.extend(
        (
            f"Preserved user data: {result['user_data_dir']}",
            "Preserved the bootstrap pointer.",
            f"Remove the Ricky software with: {result['software_removal_command']}",
        )
    )
    return "\n".join(lines)


def _render_upgrade_check(result: UpgradeCheckResult) -> str:
    lines = [
        f"Current Ricky software: {result.current_software_version}",
        f"Current data generation: {result.current_data_generation}",
        f"Upgrade status: {result.status}",
        result.detail,
    ]
    if result.selected_release is not None:
        lines.insert(2, f"Selected release: {result.selected_release.software_version}")
    if result.inventory:
        lines.append("Durable-state inventory:")
        for item in result.inventory:
            found = "none" if item.found_schema_version is None else str(item.found_schema_version)
            lines.append(
                f"  {item.target.adapter_id}/{item.target.target_id}: {item.state} "
                f"(found {found}, target {item.target_schema_version}) "
                f"{item.target.path}"
            )
    return "\n".join(lines)


def _release_resolver(paths: list[Path]) -> ReleaseResolver:
    if paths:
        return LocalReleaseResolver(tuple(paths))
    return GitHubReleaseResolver()


async def _run_upgrade_apply(
    *,
    target: str | None,
    yes: bool,
    update_jobs: bool,
    as_json: bool,
    release_descriptor: list[Path],
    renderer: CliRenderer,
) -> UpgradeJournal:
    environment = discover_installed_tool_environment()
    resolver = _release_resolver(release_descriptor)
    checked = await check_upgrade(resolver, target)
    if checked.status != "update_available" or checked.selected_release is None:
        raise InstallationError(checked.detail)
    target_release = checked.selected_release
    source_release = await resolver.resolve(environment.current_version)
    if source_release is None:
        raise InstallationError(
            "the exact installed release artifacts are unavailable; rollback cannot be guaranteed"
        )
    if source_release.software_version != environment.current_version:
        raise InstallationError("source release descriptor does not match installed Ricky")

    with installation_operation_lock(
        mode="shared",
        timeout_seconds=5.0,
        operation="upgrade_preflight",
    ):
        pointer, manifest = require_compatible_installation()
        root = Path(pointer.user_data_dir)
        preview_registry = build_upgrade_registry(root)
        preview_plan = preview_registry.build_plan(
            source_data_generation=manifest.data_generation,
            target_data_generation=target_release.target_data_generation,
        )
        preview_preflights = preview_registry.preflight(user_data_dir=root)
        preview_estimate = sum(item.estimated_backup_bytes for item in preview_preflights)
    if not yes:
        renderer.render_status(
            "Ricky upgrade plan:\n"
            f"  software: {environment.current_version} -> "
            f"{target_release.software_version}\n"
            f"  data generation: {manifest.data_generation} -> "
            f"{target_release.target_data_generation}\n"
            f"  data root: {root}\n"
            f"  migration steps: {len(preview_plan.steps)}\n"
            f"  estimated mutable-state backup: {preview_estimate} bytes\n"
            f"  profile-job updates: {'enabled' if update_jobs else 'disabled'}\n"
            "  an active managed gateway will be stopped and restarted",
            style="yellow",
        )
        if not typer.confirm("Apply this released Ricky upgrade?", default=False):
            raise InstallationError("upgrade was cancelled before any lifecycle change")

    operation_id = uuid4().hex
    software_binding = await create_software_binding(
        user_data_dir=root,
        operation_id=operation_id,
        source_release=source_release,
        target_release=target_release,
    )
    operation_root = root / "upgrades" / operation_id
    settings = load_settings()
    managed = None
    prepared = False
    try:
        managed = await prepare_managed_upgrade(
            settings=settings,
            source_executable=Path(environment.executable),
            update_jobs=update_jobs,
        )
        with installation_operation_lock(
            mode="exclusive",
            timeout_seconds=30.0,
            operation="upgrade",
            operation_id=operation_id,
        ) as lock:
            locked_pointer, locked_manifest = require_installation()
            if locked_pointer != pointer or locked_manifest != manifest:
                raise InstallationError("Ricky installation changed while preparing the upgrade")
            locked_target = await resolver.resolve(target_release.software_version)
            locked_source = await resolver.resolve(source_release.software_version)
            if locked_target != target_release or locked_source != source_release:
                raise InstallationError("release metadata changed while preparing the upgrade")

            registry = build_upgrade_registry(root)
            plan = registry.build_plan(
                source_data_generation=manifest.data_generation,
                target_data_generation=target_release.target_data_generation,
            )
            if plan != preview_plan:
                raise InstallationError("upgrade migration plan changed after confirmation")
            registry.preflight(user_data_dir=root)

            software = UvToolSoftwareController(
                environment=environment,
                lock=lock,
                as_json=as_json,
            )
            coordinator = UpgradeCoordinator(
                user_data_dir=root,
                lock=lock,
                registry=registry,
                software=software,
                integrations=ManagedUpgradeController(
                    user_data_dir=root,
                    executable=Path(environment.executable),
                    # Reconciliation runs inside the asyncio.to_thread worker
                    # below, which owns no event loop of its own.
                    run_async=asyncio.run,
                ),
            )
            coordinator.prepare(
                target_software_version=target_release.software_version,
                target_data_generation=target_release.target_data_generation,
                operation_id=operation_id,
                software=software_binding,
                managed=managed,
            )
            prepared = True
            journal = await asyncio.to_thread(coordinator.resume)
        restart_gateway_after_upgrade(
            user_data_dir=root,
            executable=Path(environment.executable),
            operation_id=journal.operation_id,
        )
        return journal
    except BaseException as exc:
        # A handed-off child owns this directory and the operation journal. Only
        # remove a pre-journal cache that never became an active installation.
        try:
            current = require_installation()[1]
        except (InstallationError, OSError):
            current = None
        if (
            current is not None
            and current.operation_id != operation_id
            and not (operation_root / "journal.json").exists()
        ):
            shutil.rmtree(operation_root, ignore_errors=True)
        if managed is not None and not prepared:
            try:
                restore_gateway_after_aborted_prepare(
                    settings=settings,
                    executable=Path(environment.executable),
                    managed=managed,
                )
            except Exception as restore_failure:
                # The abort cause is what the operator must act on, so report a
                # failed best-effort restore as context instead of replacing it.
                exc.add_note(
                    "the managed gateway was stopped for this upgrade and could not "
                    f"be restarted: {restore_failure}"
                )
        raise


def _run_upgrade_recovery(*, rollback: bool, as_json: bool) -> UpgradeJournal:
    pointer, manifest = require_installation()
    if manifest.operation_id is None:
        raise InstallationError("installation has no active upgrade operation")
    environment = discover_installed_tool_environment()
    root = Path(pointer.user_data_dir)
    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=30.0,
        operation="upgrade_rollback" if rollback else "upgrade_resume",
        operation_id=manifest.operation_id,
    ) as lock:
        software = UvToolSoftwareController(
            environment=environment,
            lock=lock,
            as_json=as_json,
        )
        coordinator = UpgradeCoordinator(
            user_data_dir=root,
            lock=lock,
            registry=build_upgrade_registry(root),
            software=software,
            integrations=ManagedUpgradeController(
                user_data_dir=root,
                executable=Path(environment.executable),
                run_async=asyncio.run,
            ),
        )
        journal = coordinator.rollback() if rollback else coordinator.resume()
    restart_gateway_after_upgrade(
        user_data_dir=root,
        executable=Path(environment.executable),
        operation_id=journal.operation_id,
    )
    return journal


def _upgrade_result(journal: UpgradeJournal) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": journal.state,
        "operation_id": journal.operation_id,
        "source_software_version": str(journal.source_software_version),
        "target_software_version": str(journal.target_software_version),
        "source_data_generation": journal.source_data_generation,
        "target_data_generation": journal.target_data_generation,
        "backup_manifest": journal.backup.manifest_path,
    }
    if journal.managed is not None:
        result["managed"] = load_managed_result(
            Path(journal.user_data_dir), journal.operation_id
        ).model_dump(mode="json")
    return result


def _render_upgrade_result(journal: UpgradeJournal) -> str:
    # Only a settled operation may be reported.  Treating every non-completed
    # state as a rollback is how a half-finished recovery once announced that it
    # had rolled back while its data was still partly migrated.
    if journal.state not in {"completed", "rolled_back"}:
        raise InstallationError(
            f"upgrade operation {journal.operation_id} is still {journal.state}; "
            "run `ricky upgrade --resume` or `ricky upgrade --rollback --yes`"
        )
    completed = journal.state == "completed"
    outcome = "Upgraded" if completed else "Rolled back"
    software = journal.target_software_version if completed else journal.source_software_version
    generation = journal.target_data_generation if completed else journal.source_data_generation
    rendered = (
        f"{outcome} Ricky installation.\n"
        f"Software: {software}\n"
        f"Data generation: {generation}\n"
        f"Backup manifest: {journal.backup.manifest_path}\n"
        f"Operation: {journal.operation_id}"
    )
    if journal.managed is None:
        return rendered
    managed = load_managed_result(Path(journal.user_data_dir), journal.operation_id)
    schedule_counts = {
        "installed": len(managed.schedules_installed),
        "ready": len(managed.schedules_ready),
        "approval required": len(managed.schedules_approval_required),
        "validation required": len(managed.schedules_validation_required),
        "lineage required": len(managed.schedules_lineage_required),
        "unavailable": len(managed.schedules_unavailable),
        "disabled": len(managed.schedules_disabled),
    }
    schedule_summary = (
        ", ".join(f"{count} {state}" for state, count in schedule_counts.items() if count) or "none"
    )
    return (
        rendered + f"\nGateway: {managed.gateway}\nSchedules: {schedule_summary}\n{managed.detail}"
    )


def _validate_recovery_request(
    *,
    resume: bool,
    rollback: bool,
    yes: bool,
    as_json: bool,
) -> None:
    if resume and yes:
        raise InstallationError("--yes is not valid with --resume")
    if rollback and not yes:
        if as_json:
            raise InstallationError("--json rollback requires --yes")
        raise InstallationError("rollback requires --yes")
    _pointer, manifest = require_installation()
    if manifest.migration_state == "clean":
        raise InstallationError("there is no upgrade operation to recover")


def _emit(
    result: BaseModel | dict[str, Any],
    renderer: CliRenderer,
    *,
    as_json: bool,
    human: str,
) -> None:
    emit_result(result, renderer, as_json=as_json, human=human)


def _fail(exc: BaseException, renderer: CliRenderer, *, as_json: bool) -> Never:
    fail(exc, renderer, as_json=as_json, prefix="Installation error")
