"""Provider-free gateway transport, inbox, and operations commands."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Coroutine
from typing import Annotated, Any

import typer
from pydantic import SecretStr

from ricky.agent.events import AgentEvent
from ricky.config import RickySettings, load_settings, user_data_path
from ricky.gateway import ConversationCoordinator, GatewayService, GatewayStore
from ricky.gateway.audit import AuditChain, GatewayAudit
from ricky.gateway.health import DoctorReport, GatewayHealth, GatewayStatus
from ricky.gateway.lock import GatewayLock, GatewayLockError
from ricky.gateway.recovery import GatewayRecovery, RecoveryPlan
from ricky.gateway.retention import GatewayRetention, RetentionPlan
from ricky.gateway.service import ServiceEvent
from ricky.gateway.service_unit import MARKER, GatewayServiceUnit, ServiceUnitError
from ricky.gateway.vault_bootstrap import (
    STARTUP_UNLOCK_FAILURE_EXIT_CODE,
    GatewayVaultBootstrapError,
    GatewayVaultBootstrapServer,
    consume_gateway_vault_bootstrap,
)
from ricky.interfaces.cli.gateway_events import GatewayEventRenderer
from ricky.interfaces.cli.render import CliRenderer
from ricky.interfaces.messaging.telegram import (
    TelegramTransport,
    TelegramTransportError,
    split_telegram_text,
)
from ricky.messaging.runtime import MessagingRuntime, MessagingRuntimeError
from ricky.messaging.store import MessagingStore, MessagingStoreError
from ricky.messaging.types import InboundMessage
from ricky.notifications import NotificationStoreError
from ricky.notifications.routes import RoutePolicy
from ricky.profiles import ProfileScope, validate_profile_name
from ricky.protected_values import ResidentProtectedValueRegistry, UnlockRequest


def register_gateway_commands(gateway_app: typer.Typer) -> None:
    """Attach the gateway transport command tree to the main CLI composition root."""

    transport_app = typer.Typer(
        help="Diagnose, poll, and deliver messaging transports.",
        no_args_is_help=True,
        add_completion=False,
    )
    doctor_app = typer.Typer(no_args_is_help=True, add_completion=False)
    poll_app = typer.Typer(no_args_is_help=True, add_completion=False)
    inbox_app = typer.Typer(
        help="Inspect and reply to the durable inbound message queue.",
        no_args_is_help=True,
        add_completion=False,
    )
    service_app = typer.Typer(
        help="Manage the supervised systemd --user gateway service.",
        no_args_is_help=True,
        add_completion=False,
    )
    gateway_app.add_typer(transport_app, name="transport")
    gateway_app.add_typer(inbox_app, name="inbox")
    gateway_app.add_typer(service_app, name="service")
    transport_app.add_typer(doctor_app, name="doctor")
    transport_app.add_typer(poll_app, name="poll")

    @gateway_app.command("status")
    def gateway_status() -> None:
        """Report durable gateway state without calling any provider."""

        _run_gateway_command(_status)

    @gateway_app.command("doctor")
    def gateway_doctor() -> None:
        """Check configuration, storage, routes, and supervision offline."""

        _run_gateway_command(_doctor)

    @gateway_app.command("recover")
    def gateway_recover(
        dry_run: bool = typer.Option(True, "--dry-run/--apply"),
    ) -> None:
        """Inspect, or with --apply repair, every interrupted durable record."""

        _run_gateway_command(lambda renderer: _recover(dry_run, renderer))

    @gateway_app.command("prune")
    def gateway_prune(
        dry_run: bool = typer.Option(True, "--dry-run/--apply"),
    ) -> None:
        """List, or with --apply remove, finished unreferenced evidence."""

        _run_gateway_command(lambda renderer: _prune(dry_run, renderer))

    @gateway_app.command("audit")
    def gateway_audit(correlation_id: str = typer.Argument(...)) -> None:
        """Follow one canonical id across every subsystem that owns a link."""

        _run_gateway_command(lambda renderer: _audit(correlation_id, renderer))

    @service_app.command("show")
    def service_show() -> None:
        """Print the exact unit that install would write."""

        _run_gateway_command(_service_show)

    @service_app.command("install")
    def service_install(
        enable: bool = typer.Option(True, "--enable/--no-enable"),
    ) -> None:
        """Write the managed unit, reload the user manager, and optionally enable it."""

        _run_gateway_command(lambda renderer: _service_install(enable, renderer))

    @service_app.command("uninstall")
    def service_uninstall() -> None:
        """Stop, disable, and remove only a Ricky-owned unit."""

        _run_gateway_command(_service_uninstall)

    @service_app.command("start")
    def service_start(
        unlock_vault: Annotated[
            list[str] | None,
            typer.Option(
                "--unlock-vault",
                metavar="PROFILE",
                help="Prompt once and unlock this profile vault in the new gateway process.",
            ),
        ] = None,
    ) -> None:
        """Start the installed user service."""

        profiles = tuple(unlock_vault or ())
        _run_gateway_command(
            lambda renderer: _service_control("start", renderer, unlock_vault=profiles)
        )

    @service_app.command("stop")
    def service_stop() -> None:
        """Stop the installed user service."""

        _run_gateway_command(lambda renderer: _service_control("stop", renderer))

    @service_app.command("restart")
    def service_restart(
        unlock_vault: Annotated[
            list[str] | None,
            typer.Option(
                "--unlock-vault",
                metavar="PROFILE",
                help="Prompt once and unlock this profile vault in the replacement gateway.",
            ),
        ] = None,
    ) -> None:
        """Restart the installed user service."""

        profiles = tuple(unlock_vault or ())
        _run_gateway_command(
            lambda renderer: _service_control("restart", renderer, unlock_vault=profiles)
        )

    @service_app.command("status")
    def service_status() -> None:
        """Report whether the installed user service is active."""

        _run_gateway_command(lambda renderer: _service_control("status", renderer))

    @gateway_app.command("run")
    def run_gateway(
        unlock_vault: Annotated[
            list[str] | None,
            typer.Option(
                "--unlock-vault",
                metavar="PROFILE",
                help="Prompt once and keep this profile vault unlocked until gateway exit.",
            ),
        ] = None,
    ) -> None:
        """Run the persistent foreground gateway until interrupted."""

        profiles = tuple(unlock_vault or ())
        _run_gateway_command(lambda renderer: _run_service(profiles, renderer))

    @gateway_app.command("process")
    def process_gateway(once: bool = typer.Option(False, "--once")) -> None:
        """Process one bounded pending-inbox batch without polling."""

        if not once:
            raise typer.BadParameter("only --once is supported by this command")
        _run_gateway_command(_process_once)

    @doctor_app.command("telegram")
    def doctor_telegram(account: str = typer.Argument(...)) -> None:
        """Explicitly call Telegram getMe for one configured account."""

        _run_gateway_command(lambda renderer: _doctor_telegram(account, renderer))

    @poll_app.command("telegram")
    def poll_telegram(
        account: str = typer.Argument(...),
        once: bool = typer.Option(False, "--once"),
    ) -> None:
        """Long-poll Telegram once and atomically persist the resulting batch."""

        if not once:
            raise typer.BadParameter("only --once is supported by this command")
        _run_gateway_command(lambda renderer: _poll_telegram(account, renderer))

    @transport_app.command("deliver")
    def deliver(once: bool = typer.Option(False, "--once")) -> None:
        """Deliver one bounded batch from the durable notification outbox."""

        if not once:
            raise typer.BadParameter("only --once is supported by this command")
        _run_gateway_command(_deliver)

    @inbox_app.command("list")
    def inbox_list(
        status: str | None = typer.Option(None, "--status"),
        limit: int = typer.Option(50, "--limit", min=1, max=1_000),
    ) -> None:
        """List durable inbound messages without provider or network access."""

        _run_gateway_command(lambda renderer: _inbox_list(status, limit, renderer))

    @inbox_app.command("show")
    def inbox_show(message_id: str = typer.Argument(...)) -> None:
        """Show one durable inbound message without provider or network access."""

        _run_gateway_command(lambda renderer: _inbox_show(message_id, renderer))

    @inbox_app.command("reply")
    def inbox_reply(
        message_id: str = typer.Argument(...),
        text: str = typer.Option(..., "--text"),
    ) -> None:
        """Durably enqueue and attempt delivery of a trusted inbound reply."""

        _run_gateway_command(lambda renderer: _inbox_reply(message_id, text, renderer))

    @inbox_app.command("dismiss")
    def inbox_dismiss(message_id: str = typer.Argument(...)) -> None:
        """Acknowledge pending or uncertain inbox work without running it."""

        _run_gateway_command(lambda renderer: _inbox_dismiss(message_id, renderer))


async def _run_service(unlock_vault: tuple[str, ...], renderer: CliRenderer) -> None:
    bootstrap_settings = load_settings()
    profiles = _normalize_unlock_profiles(bootstrap_settings, unlock_vault)
    protected_values = ResidentProtectedValueRegistry(bootstrap_settings)

    async def startup() -> None:
        try:
            if profiles:
                passphrases = await _prompt_vault_passphrases(
                    bootstrap_settings,
                    profiles,
                    renderer,
                    registry=protected_values,
                )
                try:
                    await protected_values.unlock_many(passphrases)
                finally:
                    passphrases.clear()
            else:
                await consume_gateway_vault_bootstrap(
                    bootstrap_settings,
                    protected_values,
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise GatewayVaultBootstrapError("requested gateway vault unlock failed") from exc
        renderer.render_status("Ricky foreground gateway is running.", style="green")

    events = GatewayEventRenderer(sys.stdout)
    try:
        await _gateway_service(
            settings=bootstrap_settings,
            service_event_sink=events.render_service,
            agent_event_sink=events.render_agent,
            protected_values=protected_values,
            startup_hook=startup,
        ).run()
    finally:
        await protected_values.aclose()


async def _process_once(renderer: CliRenderer) -> None:
    count = await _gateway_service().process_once()
    renderer.render_status(f"Processed {count} inbound message(s).", style="green")


async def _doctor_telegram(account: str, renderer: CliRenderer) -> None:
    settings = _operator_runtime_settings(load_settings())
    config = settings.messaging.telegram_accounts.get(account)
    if config is None:
        raise MessagingRuntimeError(f"unknown Telegram account: {account}")
    if not config.enabled:
        raise MessagingRuntimeError(f"Telegram account {account!r} is disabled")
    transport = TelegramTransport(account, config)
    try:
        identity = await transport.doctor()
    finally:
        await transport.aclose()
    username = f"@{identity.username}" if identity.username is not None else "(none)"
    renderer.render_status(
        f"Telegram account: {account}\nBot id: {identity.id}\n"
        f"Username: {username}\nName: {identity.display_name}",
        style="green",
    )


async def _poll_telegram(account: str, renderer: CliRenderer) -> None:
    runtime = _messaging_runtime()
    messages = await runtime.poll_once(account)
    accepted = sum(message.status == "pending" for message in messages)
    rejected = sum(message.status == "rejected" for message in messages)
    renderer.render_status(
        f"Stored {len(messages)} update(s): {accepted} accepted, {rejected} rejected.",
        style="green",
    )


async def _deliver(renderer: CliRenderer) -> None:
    count = await _messaging_runtime().deliver_once()
    renderer.render_status(f"Delivered {count} notification(s).", style="green")


async def _inbox_list(status: str | None, limit: int, renderer: CliRenderer) -> None:
    store = MessagingStore(load_settings())
    await store.initialize()
    messages = await store.list_inbox(status=status, limit=limit)
    if not messages:
        renderer.render_status("No inbox messages found.", style="yellow")
        return
    renderer.render_status("\n\n".join(_render_inbound(item) for item in messages), style="")


async def _inbox_show(message_id: str, renderer: CliRenderer) -> None:
    store = MessagingStore(load_settings())
    await store.initialize()
    renderer.render_status(_render_inbound(await store.get_inbox(message_id)), style="")


async def _inbox_reply(message_id: str, text: str, renderer: CliRenderer) -> None:
    runtime = _messaging_runtime()
    outbox_id = await runtime.enqueue_reply(message_id, text)
    delivered = await runtime.deliver_once()
    renderer.render_status(
        f"Reply outbox: {outbox_id}\nDelivered in this pass: {delivered}",
        style="green",
    )


async def _inbox_dismiss(message_id: str, renderer: CliRenderer) -> None:
    store = MessagingStore(load_settings())
    await store.initialize()
    dismissed = await store.dismiss_inbox(message_id)
    renderer.render_status(f"Dismissed inbox message: {dismissed.id}", style="green")


async def _status(renderer: CliRenderer) -> None:
    status = await GatewayHealth(_operator_runtime_settings(load_settings())).status()
    renderer.render_status(_render_status(status), style="")


async def _doctor(renderer: CliRenderer) -> None:
    report = await GatewayHealth(_operator_runtime_settings(load_settings())).doctor()
    renderer.render_status(_render_doctor(report), style="")
    if not report.ok:
        raise typer.Exit(1)


async def _recover(dry_run: bool, renderer: CliRenderer) -> None:
    settings = load_settings()
    recovery = GatewayRecovery(settings, scope=_operator_profile_scope(settings))
    plan = await (recovery.inspect() if dry_run else recovery.apply())
    renderer.render_status(_render_recovery(plan), style="")


async def _prune(dry_run: bool, renderer: CliRenderer) -> None:
    settings = load_settings()
    retention = GatewayRetention(settings, scope=_operator_profile_scope(settings))
    plan = await (retention.plan() if dry_run else retention.apply())
    renderer.render_status(_render_retention(plan), style="")


async def _audit(correlation_id: str, renderer: CliRenderer) -> None:
    settings = load_settings()
    chain = await GatewayAudit(
        settings,
        scope=_operator_profile_scope(settings),
    ).trace(correlation_id)
    renderer.render_status(_render_audit(chain), style="")


async def _service_show(renderer: CliRenderer) -> None:
    unit = GatewayServiceUnit(load_settings())
    renderer.render_status(
        f"unit path: {unit.unit_path}\nlog file: {unit.log_path}\n\n{unit.render()}",
        style="",
    )


async def _service_install(enable: bool, renderer: CliRenderer) -> None:
    unit = GatewayServiceUnit(load_settings())
    result = unit.install()
    lines = [
        f"unit: {result.unit_path}",
        f"log file: {unit.log_path}",
        f"created: {result.created}",
        f"verified: {result.verified}",
    ]
    if result.backup_path is not None:
        lines.append(f"backup: {result.backup_path}")
    if not result.verified:
        renderer.render_error("\n".join([*lines, "installed bytes do not match the rendering"]))
        raise typer.Exit(1)
    reload_result = unit.daemon_reload()
    lines.append(f"daemon-reload: exit {reload_result.returncode}")
    if enable:
        enabled = unit.enable()
        lines.append(f"enable: exit {enabled.returncode}")
    renderer.render_status("\n".join(lines), style="green")


async def _service_uninstall(renderer: CliRenderer) -> None:
    unit = GatewayServiceUnit(load_settings())
    # One read decides ownership, so a unit cannot be swapped between the check
    # and the removal, and no foreign unit is ever touched.
    installed = unit.installed()
    if installed is not None and not installed.startswith(MARKER):
        raise ServiceUnitError(
            f"{unit.unit_path} was not written by Ricky; no service action was taken"
        )
    lines: list[str] = []
    if installed is None:
        # The user manager can still hold the unit loaded and running after the
        # file is removed out of band, so the teardown always runs.
        lines.append("No installed Ricky gateway unit file was present.")
    lines.append(f"stop: exit {unit.stop().returncode}")
    lines.append(f"disable: exit {unit.disable().returncode}")
    lines.append(f"removed: {False if installed is None else unit.uninstall()}")
    lines.append(f"daemon-reload: exit {unit.daemon_reload().returncode}")
    renderer.render_status("\n".join(lines), style="green")


async def _service_control(
    action: str,
    renderer: CliRenderer,
    *,
    unlock_vault: tuple[str, ...] = (),
) -> None:
    settings = load_settings()
    unit = GatewayServiceUnit(settings)
    operations = {
        "start": unit.start,
        "stop": unit.stop,
        "restart": unit.restart,
        "status": unit.status,
    }
    if unlock_vault:
        if action not in {"start", "restart"}:
            raise ServiceUnitError("vault unlock is available only for service start or restart")
        if action == "start" and (
            (await _joined_thread(unit.status)).returncode == 0 or GatewayLock(settings).is_active()
        ):
            raise ServiceUnitError(
                "the gateway is already active; use gateway service restart --unlock-vault"
            )
        profiles = _normalize_unlock_profiles(settings, unlock_vault)
        probe = ResidentProtectedValueRegistry(settings)
        try:
            passphrases = await _prompt_vault_passphrases(
                settings,
                profiles,
                renderer,
                registry=probe,
            )
        finally:
            await probe.aclose()
        launched = False
        try:
            async with GatewayVaultBootstrapServer(settings, passphrases) as bootstrap:
                if action == "start" and (
                    (await _joined_thread(unit.status)).returncode == 0
                    or GatewayLock(settings).is_active()
                ):
                    raise ServiceUnitError(
                        "the gateway became active while waiting for vault input; "
                        "use gateway service restart --unlock-vault"
                    )
                exchange = asyncio.create_task(bootstrap.exchange())
                try:
                    control = asyncio.create_task(asyncio.to_thread(operations[action]))
                    try:
                        result = await asyncio.shield(control)
                    except asyncio.CancelledError as cancelled:
                        result = await asyncio.shield(control)
                        launched = result.returncode == 0
                        raise cancelled
                    launched = result.returncode == 0
                    if not launched:
                        exchange.cancel()
                        await asyncio.gather(exchange, return_exceptions=True)
                        raise ServiceUnitError(
                            f"systemd gateway {action} failed with exit {result.returncode}"
                        )
                    else:
                        await exchange
                except BaseException:
                    exchange.cancel()
                    await asyncio.gather(exchange, return_exceptions=True)
                    raise
        except BaseException:
            if launched:
                await _joined_thread(unit.stop)
            raise
        finally:
            passphrases.clear()
    else:
        result = operations[action]()
    body = (result.stdout or result.stderr).strip() or "(no output)"
    renderer.render_status(
        f"{' '.join(result.args)}\nexit: {result.returncode}\n{body}",
        style="green" if result.returncode == 0 else "yellow",
    )


def _render_status(status: GatewayStatus) -> str:
    lines = [
        f"generated: {status.generated_at.isoformat()}",
        f"gateway enabled: {status.gateway_enabled}",
    ]
    if status.lock_owner is not None:
        age = "unknown" if status.lock_age_seconds is None else f"{status.lock_age_seconds:.0f}s"
        lines.append(
            f"lock: pid {status.lock_owner.pid} on {status.lock_owner.host}, "
            f"active {status.lock_active}, age {age}"
        )
    else:
        lines.append("lock: never taken")
    for transport in status.transports:
        cursor = (
            "never" if transport.last_cursor_at is None else transport.last_cursor_at.isoformat()
        )
        lines.append(
            f"transport {transport.transport}/{transport.account}: "
            f"enabled {transport.enabled}, credential {transport.credential_configured}, "
            f"last cursor {cursor}, polling {transport.poller_leased}"
        )
    lines.append(f"inbox: {_counts(status.inbox)}")
    if status.oldest_pending_inbox_at is not None:
        lines.append(f"oldest pending inbox: {status.oldest_pending_inbox_at.isoformat()}")
    lines.append(f"conversations: {_counts(status.conversations)}")
    lines.append(f"turns: {_counts(status.turns)}")
    lines.append(f"sessions: {_counts(status.sessions)}")
    lines.append(f"executions: {_counts(status.executions)}")
    lines.append(f"outbox: {_counts(status.outbox)}")
    if status.oldest_pending_outbox_at is not None:
        lines.append(f"oldest pending outbox: {status.oldest_pending_outbox_at.isoformat()}")
    lines.append(f"effects: {_counts(status.effects)}")
    lines.append(f"uncertain: {status.uncertain_count}   in_doubt: {status.in_doubt_count}")
    if status.recent_errors:
        lines.append("recent errors:")
        lines.extend(f"  {error}" for error in status.recent_errors)
    return "\n".join(lines)


def _render_doctor(report: DoctorReport) -> str:
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    lines = [f"generated: {report.generated_at.isoformat()}"]
    lines.extend(f"[{marks[check.status]}] {check.name}: {check.detail}" for check in report.checks)
    lines.append("")
    lines.append(f"{len(report.failures)} failing check(s)")
    return "\n".join(lines)


def _render_recovery(plan: RecoveryPlan) -> str:
    mode = "APPLIED" if plan.applied else "dry run (no state changed)"
    lines = [f"generated: {plan.generated_at.isoformat()}", f"mode: {mode}"]
    if not plan.actions:
        lines.append("no interrupted records were found")
    for action in plan.actions:
        lines.append(
            f"{action.subsystem} {action.record_id}: {action.from_state} -> {action.to_state} "
            f"({action.reason})"
        )
    if plan.failures:
        lines.append("")
        lines.append("subsystems that could not be inspected:")
        lines.extend(f"  {failure}" for failure in plan.failures)
    return "\n".join(lines)


def _render_retention(plan: RetentionPlan) -> str:
    mode = "APPLIED" if plan.applied else "dry run (nothing deleted)"
    lines = [
        f"generated: {plan.generated_at.isoformat()}",
        f"mode: {mode}",
        f"retention enabled: {plan.enabled}",
    ]
    for group in plan.groups:
        lines.append("")
        lines.append(
            f"{group.name}: keep {group.keep}, removable "
            f"{len(group.removable_ids) + len(group.removable_paths)}, "
            f"protected {len(group.protected_ids)}, removed {group.removed}"
        )
        lines.extend(f"  {item}" for item in group.removable_ids[:50])
        lines.extend(f"  {item}" for item in group.removable_paths[:50])
    return "\n".join(lines)


def _render_audit(chain: AuditChain) -> str:
    lines = [
        f"generated: {chain.generated_at.isoformat()}",
        f"query: {chain.query} ({chain.resolved_kind or 'unrecognised'})",
        "",
    ]
    for link in chain.links:
        at = "" if link.at is None else f" at {link.at.isoformat()}"
        status = "" if link.status is None else f" [{link.status}]"
        identity = link.id or "-"
        lines.append(f"{link.kind:<18} {link.state:<10} {identity}{status}{at}")
        lines.append(f"{'':<18} {'':<10} {link.detail}")
    return "\n".join(lines)


def _counts(values: dict[str, int]) -> str:
    if not values:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(values.items()))


def _operator_profile_scope(settings: RickySettings) -> ProfileScope:
    return settings.resolve_profile_scope(
        settings.profiles.default,
        access_profiles=settings.profiles.enabled,
    )


def _operator_runtime_settings(settings: RickySettings) -> RickySettings:
    """Add every enabled Telegram account without pre-resolving route policy."""

    operator_accounts = settings.resolve_profile_runtime_settings(
        _operator_profile_scope(settings)
    ).messaging.telegram_accounts
    messaging = settings.messaging.model_copy(
        update={"telegram_accounts": operator_accounts},
        deep=True,
    )
    return settings.model_copy(update={"messaging": messaging}, deep=True)


def _messaging_runtime(settings: RickySettings | None = None) -> MessagingRuntime:
    settings = settings or _operator_runtime_settings(load_settings())

    def factory(account: str) -> TelegramTransport:
        config = settings.messaging.telegram_accounts.get(account)
        if config is None:
            raise MessagingRuntimeError(f"unknown Telegram account: {account}")
        return TelegramTransport(account, config, user_data_root=user_data_path(settings))

    return MessagingRuntime(
        settings,
        routes=RoutePolicy(settings, conversation_resolver=GatewayStore(settings)),
        transport_factory=factory,
        text_splitter=split_telegram_text,
    )


def _gateway_service(
    *,
    settings: RickySettings | None = None,
    service_event_sink: Callable[[ServiceEvent], Awaitable[None] | None] | None = None,
    agent_event_sink: Callable[[AgentEvent], Awaitable[None] | None] | None = None,
    protected_values: ResidentProtectedValueRegistry | None = None,
    startup_hook: Callable[[], Awaitable[None]] | None = None,
) -> GatewayService:
    bootstrap_settings = settings or load_settings()
    operator_settings = _operator_runtime_settings(bootstrap_settings)
    gateway = GatewayStore(bootstrap_settings)
    conversations = ConversationCoordinator(
        bootstrap_settings,
        gateway=gateway,
        event_sink=agent_event_sink,
        protected_value_registry=protected_values,
    )
    return GatewayService(
        operator_settings,
        messaging=_messaging_runtime(operator_settings),
        conversations=conversations,
        event_sink=service_event_sink,
        protected_values=protected_values,
        startup_hook=startup_hook,
    )


def _normalize_unlock_profiles(
    settings: RickySettings,
    profiles: tuple[str, ...],
) -> tuple[str, ...]:
    if not profiles:
        return ()
    if not settings.protected_values.enabled:
        raise GatewayVaultBootstrapError("protected values are disabled")
    normalized = tuple(validate_profile_name(profile) for profile in profiles)
    if len(normalized) != len(set(normalized)):
        raise GatewayVaultBootstrapError("gateway vault unlock profiles must be unique")
    unavailable = tuple(
        profile for profile in normalized if profile not in settings.profiles.enabled
    )
    if unavailable:
        raise GatewayVaultBootstrapError(
            "gateway vault unlock profile is unknown or disabled: " + ", ".join(unavailable)
        )
    return normalized


async def _prompt_vault_passphrases(
    settings: RickySettings,
    profiles: tuple[str, ...],
    renderer: CliRenderer,
    *,
    registry: ResidentProtectedValueRegistry,
) -> dict[str, SecretStr]:
    del settings
    for profile in profiles:
        if not await registry.initialized(profile):
            raise GatewayVaultBootstrapError(
                f"protected-value vault is not initialized for profile {profile}"
            )
    passphrases: dict[str, SecretStr] = {}
    try:
        for profile in profiles:
            supplied = await renderer.request_protected_unlock(UnlockRequest(profile=profile))
            if supplied is None:
                raise GatewayVaultBootstrapError("gateway vault unlock was cancelled")
            passphrases[profile] = supplied
        return passphrases
    except BaseException:
        passphrases.clear()
        raise


async def _joined_thread[T](operation: Callable[[], T]) -> T:
    """Join a blocking service-manager call before propagating cancellation."""

    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise


def _render_inbound(message: InboundMessage) -> str:
    lines = [
        f"message: {message.id}",
        f"status: {message.status}",
        f"transport: {message.transport}/{message.account}",
        f"update_id: {message.update_id}",
        f"destination_id: {message.destination_id}",
        f"sender_id: {message.sender_id}",
        f"platform_message_id: {message.platform_message_id}",
        f"received: {message.received_at.isoformat()}",
        f"text: {message.text}",
    ]
    if message.thread_id is not None:
        lines.append(f"thread_id: {message.thread_id}")
    if message.reply_to_platform_message_id is not None:
        lines.append(f"reply_to_platform_message_id: {message.reply_to_platform_message_id}")
    return "\n".join(lines)


def _run_gateway_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(factory(renderer))
    except GatewayVaultBootstrapError as exc:
        renderer.render_error(f"Gateway error: {exc}")
        raise typer.Exit(STARTUP_UNLOCK_FAILURE_EXIT_CODE) from exc
    except (
        GatewayLockError,
        MessagingRuntimeError,
        MessagingStoreError,
        NotificationStoreError,
        ServiceUnitError,
        TelegramTransportError,
        ValueError,
        OSError,
    ) as exc:
        renderer.render_error(f"Gateway error: {exc}")
        raise typer.Exit(1) from exc
