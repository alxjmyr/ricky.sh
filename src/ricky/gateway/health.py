"""Provider-free gateway health status and configuration doctor.

Nothing here calls a model provider, and nothing here reads a secret value. A
credential is reported only as configured or missing, never by content, so a
health snapshot is safe to paste into a bug report.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ricky.agent.session import AgentSession
from ricky.authority.store import AuthorityStore
from ricky.capabilities import (
    build_capability_registry,
    registered_skill_owners,
    validate_capability_inventory,
    validate_capability_policy,
    validate_foreground_live_policy,
)
from ricky.config import (
    RickySettings,
    ensure_private_user_data_root,
    find_project_root,
    user_data_path,
    user_data_subpath,
)
from ricky.durable_tasks.state_guard import DurableTaskStateGuard
from ricky.executions.contracts import load_contract_snapshot
from ricky.executions.store import ExecutionStore
from ricky.gateway.lock import GatewayLock, LockOwner
from ricky.gateway.service_unit import GatewayServiceUnit
from ricky.gateway.store import GatewayStore
from ricky.gateway.tools import (
    gateway_capability_inventory_tools,
    gateway_control_descriptors,
)
from ricky.jobs.store import JobRunStore
from ricky.messaging.store import MessagingStore
from ricky.notifications.routes import RouteError, RoutePolicy
from ricky.notifications.store import NotificationStore
from ricky.profiles import ProfileLabel
from ricky.project_scope import ProjectScope
from ricky.runtime.composition import CAPABILITY_SPECS, build_capability_runtime
from ricky.sessions.store import SessionStore
from ricky.tools import StateGuardRegistry

CheckStatus = Literal["ok", "warn", "fail"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransportHealth(_FrozenModel):
    """Last durable evidence that one transport account is receiving."""

    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    enabled: bool
    credential_configured: bool
    """True when a bot token is present. The value is never read or rendered."""
    last_cursor_at: datetime | None = None
    poller_leased: bool = False


class GatewayStatus(_FrozenModel):
    """One complete provider-free operational snapshot."""

    generated_at: datetime
    gateway_enabled: bool
    lock_owner: LockOwner | None = None
    lock_active: bool = False
    lock_age_seconds: float | None = Field(default=None, ge=0)
    transports: tuple[TransportHealth, ...] = ()
    inbox: dict[str, int] = Field(default_factory=dict)
    oldest_pending_inbox_at: datetime | None = None
    conversations: dict[str, int] = Field(default_factory=dict)
    turns: dict[str, int] = Field(default_factory=dict)
    sessions: dict[str, int] = Field(default_factory=dict)
    executions: dict[str, int] = Field(default_factory=dict)
    outbox: dict[str, int] = Field(default_factory=dict)
    oldest_pending_outbox_at: datetime | None = None
    effects: dict[str, int] = Field(default_factory=dict)
    uncertain_count: int = Field(default=0, ge=0)
    in_doubt_count: int = Field(default=0, ge=0)
    recent_errors: tuple[str, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("generated_at must be timezone-aware UTC")
        return value


class DoctorCheck(_FrozenModel):
    """One deterministic configuration or storage check."""

    name: str = Field(min_length=1, max_length=100)
    status: CheckStatus
    detail: str = Field(min_length=1, max_length=2_000)


class DoctorReport(_FrozenModel):
    """The complete doctor result for one configured host."""

    generated_at: datetime
    checks: tuple[DoctorCheck, ...] = ()

    @property
    def ok(self) -> bool:
        """True when no check failed. Warnings do not block operation."""

        return all(check.status != "fail" for check in self.checks)

    @property
    def failures(self) -> tuple[DoctorCheck, ...]:
        """Return only the checks that must be fixed."""

        return tuple(check for check in self.checks if check.status == "fail")


class GatewayHealth:
    """Read every durable store and configuration source without a provider."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        project_root: Path | None = None,
        messaging: MessagingStore | None = None,
        notifications: NotificationStore | None = None,
        gateway: GatewayStore | None = None,
        sessions: SessionStore | None = None,
        executions: ExecutionStore | None = None,
        jobs: JobRunStore | None = None,
        authority: AuthorityStore | None = None,
        unit: GatewayServiceUnit | None = None,
    ) -> None:
        self.settings = settings
        self.profile_scope = settings.resolve_profile_scope(
            settings.profiles.default,
            access_profiles=settings.profiles.enabled,
        )
        self.project_root = project_root
        self.messaging = messaging or MessagingStore(settings)
        self.notifications = notifications or NotificationStore(settings)
        self.gateway = gateway or GatewayStore(settings)
        self.sessions = sessions or SessionStore(settings)
        self.executions = executions or ExecutionStore(settings)
        self.jobs = jobs or JobRunStore(settings)
        self.authority = authority or AuthorityStore(settings)
        self.unit = unit or GatewayServiceUnit(settings, project_root=project_root)

    async def status(self, *, now: datetime | None = None) -> GatewayStatus:
        """Build one bounded operational snapshot from durable state only."""

        moment = now or datetime.now(UTC)
        await self._initialize()
        lock = GatewayLock(self.settings)
        owner = lock.read_owner()
        active = lock.is_active()
        age = None
        if owner is not None and active:
            age = max(0.0, (moment - owner.acquired_at).total_seconds())
        inbox = await self.messaging.inbox_counts()
        outbox = await self.notifications.outbox_counts(scope=self.profile_scope)
        turns = await self.gateway.result_counts(scope=self.profile_scope)
        effects = await self.jobs.action_counts(scope=self.profile_scope)
        errors = await self._recent_errors()
        return GatewayStatus(
            generated_at=moment,
            gateway_enabled=self.settings.gateway.enabled,
            lock_owner=owner,
            lock_active=active,
            lock_age_seconds=age,
            transports=await self._transports(moment),
            inbox=inbox,
            oldest_pending_inbox_at=await self.messaging.oldest_pending_inbox(),
            conversations=await self.gateway.conversation_counts(scope=self.profile_scope),
            turns=turns,
            sessions=await self.sessions.session_counts(scope=self.profile_scope),
            executions=await self.executions.counts(scope=self.profile_scope),
            outbox=outbox,
            oldest_pending_outbox_at=await self.notifications.oldest_pending_outbox(
                scope=self.profile_scope
            ),
            effects=effects,
            uncertain_count=(
                inbox.get("uncertain", 0)
                + turns.get("uncertain", 0)
                + (await self.executions.counts(scope=self.profile_scope)).get("uncertain", 0)
            ),
            in_doubt_count=outbox.get("in_doubt", 0) + effects.get("in_doubt", 0),
            recent_errors=errors,
        )

    async def doctor(self, *, now: datetime | None = None) -> DoctorReport:
        """Check configuration, storage, routes, and supervision without a provider."""

        moment = now or datetime.now(UTC)
        checks: list[DoctorCheck] = []
        checks.append(self._check_enabled())
        # Schemas first: initializing a store is what creates and tightens its
        # files, so a permission check before that would report a mode the very
        # same run is about to fix.
        await self._initialize()
        checks.extend(await self._check_schemas())
        checks.extend(self._check_directories())
        checks.extend(await self._check_routes())
        checks.extend(await self.capability_checks())
        checks.extend(await self._check_execution_evidence())
        checks.extend(self._check_credentials())
        checks.append(self._check_lock())
        checks.append(self._check_unit())
        return DoctorReport(generated_at=moment, checks=tuple(checks))

    async def capability_checks(self) -> tuple[DoctorCheck, ...]:
        """Validate the provider-free inventory used by doctor and startup."""

        return tuple(await self._check_capabilities())

    async def _initialize(self) -> None:
        ensure_private_user_data_root(self.settings)
        await self.messaging.initialize()
        await self.notifications.initialize()
        await self.gateway.initialize()
        await self.sessions.initialize()
        await self.executions.initialize()
        await self.jobs.initialize()
        await self.authority.initialize()

    async def _transports(self, now: datetime) -> tuple[TransportHealth, ...]:
        leased = {
            (item.transport, item.account)
            for item in await self.messaging.active_poller_leases(now=now)
        }
        health: list[TransportHealth] = []
        for account, config in sorted(self.settings.messaging.telegram_accounts.items()):
            health.append(
                TransportHealth(
                    transport="telegram",
                    account=account,
                    enabled=config.enabled,
                    credential_configured=len(config.bot_token) > 0,
                    last_cursor_at=await self.messaging.last_cursor_update("telegram", account),
                    poller_leased=("telegram", account) in leased,
                )
            )
        return tuple(health)

    async def _recent_errors(self) -> tuple[str, ...]:
        limit = self.settings.gateway.recent_error_limit
        if limit == 0:
            return ()
        errors: list[str] = []
        for request in await self.executions.list(
            scope=self.profile_scope,
            status="failed",
            limit=min(100, limit),
        ):
            if request.error is not None:
                errors.append(f"execution {request.id}: {request.error}"[:500])
        for record in await self.notifications.list(
            scope=self.profile_scope,
            status="failed",
            limit=min(100, limit),
        ):
            if record.outbox.error is not None:
                errors.append(f"outbox {record.outbox.id}: {record.outbox.error}"[:500])
        return tuple(errors[:limit])

    def _check_enabled(self) -> DoctorCheck:
        if not self.settings.gateway.enabled:
            return DoctorCheck(
                name="gateway.enabled",
                status="warn",
                detail="gateway.enabled is false; `ricky gateway run` refuses to start",
            )
        if not self.settings.gateway.routes:
            return DoctorCheck(
                name="gateway.enabled",
                status="fail",
                detail="gateway is enabled but no [gateway.routes.*] table is configured",
            )
        return DoctorCheck(
            name="gateway.enabled",
            status="ok",
            detail=f"{len(self.settings.gateway.routes)} gateway route(s) configured",
        )

    def _check_directories(self) -> list[DoctorCheck]:
        checks: list[DoctorCheck] = []
        root = user_data_path(self.settings)
        checks.append(_check_private_dir("user_data_dir", root))
        for label, configured in (
            ("gateway store", self.settings.gateway.store_path),
            ("messaging store", self.settings.messaging.store_path),
            ("sessions store", self.settings.sessions.store_path),
            ("executions store", self.settings.executions.store_path),
        ):
            path = user_data_subpath(self.settings, configured)
            checks.append(_check_private_dir(f"{label} directory", path.parent))
            if path.exists():
                checks.append(_check_private_file(f"{label} file", path))
        return checks

    async def _check_schemas(self) -> list[DoctorCheck]:
        checks: list[DoctorCheck] = []
        for name, initialize in (
            ("gateway schema", self.gateway.initialize),
            ("messaging schema", self.messaging.initialize),
            ("notification schema", self.notifications.initialize),
            ("session schema", self.sessions.initialize),
            ("execution schema", self.executions.initialize),
            ("job schema", self.jobs.initialize),
            ("authority schema", self.authority.initialize),
        ):
            try:
                await initialize()
            except Exception as exc:  # noqa: BLE001 - every schema is reported, not raised
                checks.append(DoctorCheck(name=name, status="fail", detail=str(exc)[:2_000]))
            else:
                checks.append(
                    DoctorCheck(name=name, status="ok", detail="schema is present and current")
                )
        return checks

    async def _check_routes(self) -> list[DoctorCheck]:
        checks: list[DoctorCheck] = []
        policy = RoutePolicy(self.settings, conversation_resolver=self.gateway)
        names = set(self.settings.gateway.routes)
        operator = self.settings.gateway.operator_route
        if operator is not None:
            names.add(operator)
        for name in sorted(names):
            configured = self.settings.messaging.routes.get(name)
            if configured is None:
                checks.append(
                    DoctorCheck(
                        name=f"route {name}",
                        status="fail",
                        detail=f"no [messaging.routes.{name}] target is configured",
                    )
                )
                continue
            gateway_route = self.settings.gateway.routes.get(name)
            if gateway_route is not None:
                missing_clearance = sorted(
                    set(gateway_route.profile_scope().profiles) - set(configured.accepted_profiles)
                )
                if missing_clearance:
                    checks.append(
                        DoctorCheck(
                            name=f"route {name}",
                            status="fail",
                            detail=(
                                "messaging route does not accept gateway profile(s): "
                                + ", ".join(missing_clearance)
                            ),
                        )
                    )
                    continue
            try:
                required_profiles = (
                    gateway_route.profile_scope().profiles
                    if gateway_route is not None
                    else tuple(configured.accepted_profiles)
                )
                resolved = await policy.resolve(
                    name,
                    ProfileLabel(required_profiles=required_profiles),
                )
            except (RouteError, KeyError) as exc:
                checks.append(
                    DoctorCheck(name=f"route {name}", status="fail", detail=str(exc)[:2_000])
                )
                continue
            checks.append(
                DoctorCheck(
                    name=f"route {name}",
                    status="ok",
                    detail=(
                        f"{resolved.transport}/{resolved.account}; accepts "
                        f"{', '.join(configured.accepted_profiles)}"
                    ),
                )
            )
        return checks

    async def _check_capabilities(self) -> list[DoctorCheck]:
        checks: list[DoctorCheck] = []
        routes = self.settings.gateway.routes
        if not routes:
            return checks
        for route_name, route in sorted(routes.items()):
            session = AgentSession.create(
                self.settings,
                profile_scope=route.profile_scope(),
                provider=route.provider,
                model=route.model,
            )
            if route.project_root is None:
                scope = ProjectScope.disabled()
            else:
                root = Path(route.project_root).expanduser()
                if not root.is_absolute():
                    root = (self.project_root or find_project_root()) / root
                scope = ProjectScope.bound(root)
            try:
                async with build_capability_runtime(
                    self.settings,
                    session=session,
                    project_root=scope.root,
                    project_scope=scope,
                ) as runtime:
                    registry = build_capability_registry(
                        gateway_capability_inventory_tools(
                            runtime.tools,
                            gateway_control_descriptors(),
                        ),
                        runtime.skill_registry,
                        capability_specs=CAPABILITY_SPECS,
                        skill_owners=registered_skill_owners(runtime.capability_registry),
                        state_guards=StateGuardRegistry(
                            [DurableTaskStateGuard(runtime.durable_tasks)]
                        ),
                    )
                    diagnostics = [
                        *validate_capability_inventory(
                            registry,
                            runtime.guardrail_registry,
                        ),
                        *validate_capability_policy(
                            registry,
                            runtime.guardrail_registry,
                            self.settings.agents.gateway_foreground,
                            route=route,
                        ),
                        *validate_foreground_live_policy(
                            registry,
                            self.settings.agents.gateway_foreground,
                            route=route,
                        ),
                        *validate_capability_policy(
                            registry,
                            runtime.guardrail_registry,
                            self.settings.agents.ad_hoc_background,
                            route=route,
                        ),
                    ]
            except Exception as exc:  # noqa: BLE001 - doctor reports every dependency
                checks.append(
                    DoctorCheck(
                        name=f"capability inventory {route_name}",
                        status="fail",
                        detail=str(exc)[:2_000],
                    )
                )
                continue
            errors = [item for item in diagnostics if item.severity == "error"]
            warnings = [item for item in diagnostics if item.severity == "warning"]
            checks.append(
                DoctorCheck(
                    name=f"capability inventory {route_name}",
                    status="fail" if errors else ("warn" if warnings else "ok"),
                    detail=(
                        f"{len(registry.ids())} installed; {len(errors)} error(s), "
                        f"{len(warnings)} warning(s)"
                    ),
                )
            )
            for diagnostic in (*errors, *warnings)[:20]:
                checks.append(
                    DoctorCheck(
                        name=f"capability {diagnostic.capability_id}",
                        status="fail" if diagnostic.severity == "error" else "warn",
                        detail=diagnostic.message,
                    )
                )
        return checks

    async def _check_execution_evidence(self) -> list[DoctorCheck]:
        checks: list[DoctorCheck] = []
        if self.settings.authority.enabled:
            digest = self.settings.authority.digest()
            checks.append(
                DoctorCheck(
                    name="authority policy digest",
                    status="ok",
                    detail=f"{digest[:16]}… pins every issued delegation grant",
                )
            )
        contracts = await self.executions.list_contracts(
            scope=self.profile_scope,
            limit=1_000,
        )
        snapshot_root = user_data_subpath(
            self.settings, self.settings.executions.contract_snapshot_dir
        ).resolve()
        broken = 0
        for contract in contracts:
            try:
                snapshot = load_contract_snapshot(self.settings, contract.digest)
                if snapshot != contract:
                    raise ValueError("stored and snapshotted contracts differ")
                contract_root = (snapshot_root / contract.digest).resolve()
                if not contract_root.is_relative_to(snapshot_root):
                    raise ValueError("contract snapshot escapes its configured root")
                for path in contract_root.rglob("*"):
                    if path.is_symlink() or not path.resolve().is_relative_to(contract_root):
                        raise ValueError("contract snapshot contains an escaping link")
            except Exception as exc:  # noqa: BLE001 - each broken contract is reported
                broken += 1
                checks.append(
                    DoctorCheck(
                        name=f"execution contract {contract.id}",
                        status="fail",
                        detail=str(exc)[:2_000],
                    )
                )
        checks.append(
            DoctorCheck(
                name="execution contracts",
                status="fail" if broken else "ok",
                detail=f"{len(contracts)} stored contract(s), {broken} broken snapshot(s)",
            )
        )
        incomplete = [
            grant
            for grant in await self.authority.list(
                scope=self.profile_scope,
                status="active",
                limit=1_000,
            )
            if grant.execution_request_id is None
        ]
        checks.append(
            DoctorCheck(
                name="incomplete grant attachments",
                status="fail" if incomplete else "ok",
                detail=f"{len(incomplete)} active grant(s) have no execution request",
            )
        )
        return checks

    def _check_credentials(self) -> list[DoctorCheck]:
        checks: list[DoctorCheck] = []
        for account, config in sorted(self.settings.messaging.telegram_accounts.items()):
            # len() on a SecretStr proves presence without ever reading the value.
            configured = len(config.bot_token) > 0
            if not config.enabled:
                status: CheckStatus = "warn"
                detail = "account is disabled"
            elif configured:
                status = "ok"
                detail = "bot token is configured"
            else:
                status = "fail"
                detail = "bot token is missing from the owning profile and the environment"
            checks.append(
                DoctorCheck(name=f"telegram credential {account}", status=status, detail=detail)
            )
        return checks

    def _check_lock(self) -> DoctorCheck:
        lock = GatewayLock(self.settings)
        if not lock.path.exists():
            return DoctorCheck(
                name="process lock",
                status="ok",
                detail=f"no gateway has ever locked {lock.root}",
            )
        owner = lock.read_owner()
        if lock.is_active():
            detail = (
                f"held by pid {owner.pid} on {owner.host}"
                if owner is not None
                else "held by an unidentified process"
            )
            return DoctorCheck(name="process lock", status="ok", detail=detail)
        return DoctorCheck(
            name="process lock",
            status="ok",
            detail="lock file exists but no process holds it; the root is free",
        )

    def _check_unit(self) -> DoctorCheck:
        drift = self.unit.drift()
        if drift is None:
            return DoctorCheck(
                name="service unit",
                status="ok",
                detail="no unit installed, or the installed unit matches this configuration",
            )
        return DoctorCheck(name="service unit", status="warn", detail=drift[:2_000])


def _check_private_dir(name: str, path: Path) -> DoctorCheck:
    if not path.exists():
        return DoctorCheck(
            name=name, status="warn", detail=f"{path} does not exist yet; it is created on use"
        )
    if not path.is_dir():
        return DoctorCheck(name=name, status="fail", detail=f"{path} is not a directory")
    return _mode_check(name, path, 0o700)


def _check_private_file(name: str, path: Path) -> DoctorCheck:
    return _mode_check(name, path, 0o600)


def _mode_check(name: str, path: Path, expected: int) -> DoctorCheck:
    if os.name != "posix":  # pragma: no cover - mode checks are POSIX only.
        return DoctorCheck(name=name, status="ok", detail=f"{path} exists")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return DoctorCheck(
            name=name,
            status="fail",
            detail=(
                f"{path} is group or world accessible (mode {mode:04o}, expected {expected:04o})"
            ),
        )
    return DoctorCheck(name=name, status="ok", detail=f"{path} is private (mode {mode:04o})")
