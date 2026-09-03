"""Provider-free protected-value lifecycle and policy commands."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import typer
from pydantic import SecretStr

from ricky.config import RickySettings, load_settings
from ricky.interfaces.cli.render import CliRenderer
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.protected_values import (
    ProtectedControlKind,
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedValueBroker,
    ProtectedValueKind,
    ProtectedValueStoreError,
)
from ricky.protected_values.types import DestinationPolicyMode, MaterializationMode

_ACCESS_PROFILE_OPTION = typer.Option(None, "--access-profile")
_FIELDS_OPTION = typer.Option(
    None,
    "--field",
    help="Repeat name:control:mode:label; defaults are kind-specific.",
)
_ORIGINS_OPTION = typer.Option(None, "--origin")


def register_protected_value_commands(root: typer.Typer) -> None:
    """Attach protected-value operator commands to Ricky's root CLI."""
    policy = typer.Typer(help="Inspect or replace resource destination policy.")
    approvals = typer.Typer(help="Inspect and mutate exact destination approvals.")
    root.add_typer(policy, name="policy")
    root.add_typer(approvals, name="approvals")

    @root.command("init")
    def initialize(profile: str | None = typer.Option(None, "--profile")) -> None:
        """Initialize one profile vault from a new hidden passphrase."""
        _run(lambda settings, renderer: _initialize(settings, renderer, profile))

    @root.command("status")
    def status(profile: str | None = typer.Option(None, "--profile")) -> None:
        """Show safe vault lifecycle state without unlocking."""
        _run(lambda settings, renderer: _status(settings, renderer, profile))

    @root.command("rotate-passphrase")
    def rotate_passphrase(profile: str | None = typer.Option(None, "--profile")) -> None:
        """Unlock and rewrap the vault data key with a new hidden passphrase."""
        _run(lambda settings, renderer: _rotate(settings, renderer, profile))

    @root.command("list")
    def list_values(
        profile: str | None = typer.Option(None, "--profile"),
        access_profile: list[str] | None = _ACCESS_PROFILE_OPTION,
    ) -> None:
        """List safe descriptors in the selected profile scope."""
        _run(lambda settings, renderer: _list(settings, renderer, profile, access_profile or []))

    @root.command("show")
    def show(resource: str = typer.Argument(..., help="Qualified alias as profile/name.")) -> None:
        """Show one safe descriptor without unlocking."""
        _run(lambda settings, renderer: _show(settings, renderer, resource))

    @root.command("add")
    def add(
        resource: str = typer.Argument(..., help="New qualified alias as profile/name."),
        kind: str = typer.Option(..., "--kind"),
        label: str = typer.Option(..., "--label"),
        description: str = typer.Option("", "--description"),
        field: list[str] | None = _FIELDS_OPTION,
        destination_mode: str = typer.Option("strict", "--destination-mode"),
        origin: list[str] | None = _ORIGINS_OPTION,
    ) -> None:
        """Create a resource after hidden entry of every stored field."""
        _run(
            lambda settings, renderer: _add(
                settings,
                renderer,
                resource,
                kind,
                label,
                description,
                field or [],
                destination_mode,
                origin or [],
            )
        )

    @root.command("update")
    def update(
        resource: str = typer.Argument(..., help="Qualified alias as profile/name."),
        label: str | None = typer.Option(None, "--label"),
        description: str | None = typer.Option(None, "--description"),
        field: list[str] | None = _FIELDS_OPTION,
        replace_values: bool = typer.Option(False, "--replace-values"),
    ) -> None:
        """Revise safe metadata and optionally replace stored field values."""
        _run(
            lambda settings, renderer: _update(
                settings,
                renderer,
                resource,
                label,
                description,
                field,
                replace_values,
            )
        )

    @root.command("disable")
    def disable(resource: str = typer.Argument(...)) -> None:
        """Disable materialization while retaining encrypted data and audit."""
        _run(lambda settings, renderer: _disable(settings, renderer, resource))

    @root.command("delete")
    def delete(resource: str = typer.Argument(...)) -> None:
        """Delete current ciphertext and approvals while retaining safe audit."""
        _run(lambda settings, renderer: _delete(settings, renderer, resource))

    @policy.command("show")
    def policy_show(resource: str = typer.Argument(...)) -> None:
        """Show one resource's safe destination and use policy."""
        _run(lambda settings, renderer: _policy_show(settings, renderer, resource))

    @policy.command("set")
    def policy_set(
        resource: str = typer.Argument(...),
        mode: str = typer.Option(..., "--mode"),
        origin: list[str] | None = _ORIGINS_OPTION,
        allow_unattended: bool = typer.Option(
            False,
            "--allow-unattended/--forbid-unattended",
        ),
        max_unattended_materializations: int = typer.Option(
            0,
            "--max-unattended-materializations",
            min=0,
            max=1_000,
        ),
        allow_unattended_commit: bool = typer.Option(
            False,
            "--allow-unattended-commit/--forbid-unattended-commit",
        ),
        max_unattended_commits: int = typer.Option(
            0,
            "--max-unattended-commits",
            min=0,
            max=1_000,
        ),
        max_unattended_amount_minor: int = typer.Option(
            0,
            "--max-unattended-amount-minor",
            min=0,
            max=100_000_000,
        ),
        unattended_currency: str | None = typer.Option(None, "--unattended-currency"),
    ) -> None:
        """Replace destination and unattended-use policy with exact ceilings."""
        _run(
            lambda settings, renderer: _policy_set(
                settings,
                renderer,
                resource,
                mode,
                origin or [],
                allow_unattended=allow_unattended,
                max_unattended_materializations=max_unattended_materializations,
                allow_unattended_commit=allow_unattended_commit,
                max_unattended_commits=max_unattended_commits,
                max_unattended_amount_minor=max_unattended_amount_minor,
                unattended_currency=unattended_currency,
            )
        )

    @approvals.command("list")
    def approvals_list(resource: str = typer.Argument(...)) -> None:
        """List durable exact origin-pair approvals."""
        _run(lambda settings, renderer: _approvals_list(settings, renderer, resource))

    @approvals.command("approve")
    def approvals_approve(
        resource: str = typer.Argument(...),
        top_origin: str = typer.Option(..., "--top-origin"),
        frame_origin: str = typer.Option(..., "--frame-origin"),
    ) -> None:
        """Durably approve one exact top-level and target-frame origin pair."""
        _run(
            lambda settings, renderer: _approvals_approve(
                settings, renderer, resource, top_origin, frame_origin
            )
        )

    @approvals.command("revoke")
    def approvals_revoke(
        resource: str = typer.Argument(...),
        top_origin: str = typer.Option(..., "--top-origin"),
        frame_origin: str = typer.Option(..., "--frame-origin"),
    ) -> None:
        """Revoke one exact durable origin-pair approval."""
        _run(
            lambda settings, renderer: _approvals_revoke(
                settings, renderer, resource, top_origin, frame_origin
            )
        )


def _run(operation: Callable[[RickySettings, CliRenderer], Awaitable[None]]) -> None:
    renderer = CliRenderer()

    async def invoke() -> None:
        await operation(load_settings(), renderer)

    try:
        asyncio.run(invoke())
    except (ProtectedValueStoreError, OSError, ValueError, EOFError) as exc:
        renderer.render_error(f"Protected-values error: {exc}")
        raise typer.Exit(1) from exc


def _profile(settings: RickySettings, selected: str | None) -> str:
    return settings.resolve_profile_scope(selected).primary


def _broker(
    settings: RickySettings,
    scope: ProfileScope,
    renderer: CliRenderer,
) -> ProtectedValueBroker:
    return ProtectedValueBroker(
        settings.resolve_profile_runtime_settings(scope),
        scope=scope,
        unlock_responder=renderer.request_protected_unlock,
        secure_value_responder=renderer.request_secure_value,
        destination_responder=renderer.request_protected_destination,
    )


async def _initialize(settings: RickySettings, renderer: CliRenderer, selected: str | None) -> None:
    profile = _profile(settings, selected)
    first = await _secret(renderer, "New protected-values passphrase: ")
    second = await _secret(renderer, "Repeat new passphrase: ")
    if first != second:
        raise ValueError("new passphrases did not match")
    broker = _broker(settings, ProfileScope.create(profile), renderer)
    try:
        await broker.initialize(profile, first)
    finally:
        await broker.aclose()
    renderer.render_status(f"Initialized protected values for profile {profile}.", style="green")


async def _status(settings: RickySettings, renderer: CliRenderer, selected: str | None) -> None:
    profile = _profile(settings, selected)
    broker = _broker(settings, ProfileScope.create(profile), renderer)
    try:
        status = await broker.status(profile)
    finally:
        await broker.aclose()
    renderer.render_status(status.model_dump_json(indent=2), style="")


async def _rotate(settings: RickySettings, renderer: CliRenderer, selected: str | None) -> None:
    profile = _profile(settings, selected)
    broker = _broker(settings, ProfileScope.create(profile), renderer)
    try:
        await broker.unlock(profile)
        first = await _secret(renderer, "New protected-values passphrase: ")
        second = await _secret(renderer, "Repeat new passphrase: ")
        if first != second:
            raise ValueError("new passphrases did not match")
        await broker.rotate_passphrase(profile, first)
    finally:
        await broker.aclose()
    renderer.render_status(f"Rotated protected-values passphrase for {profile}.", style="green")


async def _list(
    settings: RickySettings,
    renderer: CliRenderer,
    selected: str | None,
    access_profiles: list[str],
) -> None:
    scope = settings.resolve_profile_scope(selected, access_profiles=access_profiles)
    broker = _broker(settings, scope, renderer)
    try:
        values = await broker.catalog()
    finally:
        await broker.aclose()
    if not values:
        renderer.render_status("No protected values are available in this profile scope.")
        return
    renderer.render_status(
        "\n".join(
            f"{item.ref.qualified}  {item.kind}  revision={item.revision}  "
            f"{'enabled' if item.enabled else 'disabled'}  {item.label}"
            for item in values
        ),
        style="",
    )


async def _show(settings: RickySettings, renderer: CliRenderer, resource: str) -> None:
    broker, ref = _resource_broker(settings, renderer, resource)
    try:
        descriptor = await _descriptor(broker, ref)
    finally:
        await broker.aclose()
    renderer.render_status(descriptor.model_dump_json(indent=2), style="")


async def _add(
    settings: RickySettings,
    renderer: CliRenderer,
    resource: str,
    raw_kind: str,
    label: str,
    description: str,
    raw_fields: list[str],
    raw_mode: str,
    origins: list[str],
) -> None:
    _require_interactive(renderer)
    ref = ProfileResourceRef.from_qualified(resource)
    kind = _kind(raw_kind)
    fields = tuple(_parse_field(item) for item in raw_fields) or _default_fields(kind)
    policy = ProtectedDestinationPolicy(
        mode=_destination_mode(raw_mode), authored_origins=tuple(origins)
    )
    if not await _confirm(
        renderer,
        f"Create {ref.qualified} ({kind}) with {len(fields)} safe field descriptor(s)?",
    ):
        renderer.render_status("Protected-value creation cancelled.", style="yellow")
        return
    values = await _read_values(renderer, ref, fields)
    broker = _broker(settings, ProfileScope.create(ref.profile), renderer)
    try:
        await broker.unlock(ref.profile)
        created = await broker.create(
            profile=ref.profile,
            name=ref.name,
            kind=kind,
            label=label,
            description=description,
            fields=fields,
            policy=policy,
            values=values,
        )
    finally:
        await broker.aclose()
    renderer.render_status(
        f"Created {created.ref.qualified} revision {created.revision}.", style="green"
    )


async def _update(
    settings: RickySettings,
    renderer: CliRenderer,
    resource: str,
    label: str | None,
    description: str | None,
    raw_fields: list[str] | None,
    replace_values: bool,
) -> None:
    _require_interactive(renderer)
    broker, ref = _resource_broker(settings, renderer, resource)
    try:
        await broker.unlock(ref.profile)
        descriptor = await _descriptor(broker, ref)
        fields = (
            tuple(_parse_field(item) for item in raw_fields)
            if raw_fields is not None
            else descriptor.fields
        )
        if not await _confirm(renderer, f"Replace revision {descriptor.revision} of {resource}?"):
            renderer.render_status("Protected-value update cancelled.", style="yellow")
            return
        values = (
            await _read_values(renderer, ref, fields)
            if replace_values or fields != descriptor.fields
            else None
        )
        updated = await broker.revise(
            descriptor,
            label=label,
            description=description,
            fields=fields,
            values=values,
        )
    finally:
        await broker.aclose()
    renderer.render_status(f"Updated {resource} to revision {updated.revision}.", style="green")


async def _disable(settings: RickySettings, renderer: CliRenderer, resource: str) -> None:
    _require_interactive(renderer)
    broker, ref = _resource_broker(settings, renderer, resource)
    try:
        await broker.unlock(ref.profile)
        descriptor = await _descriptor(broker, ref)
        if not await _confirm(renderer, f"Disable protected value {resource}?"):
            renderer.render_status("Protected-value disable cancelled.", style="yellow")
            return
        updated = await broker.set_enabled(
            ref, expected_revision=descriptor.revision, enabled=False
        )
    finally:
        await broker.aclose()
    renderer.render_status(f"Disabled {resource} at revision {updated.revision}.", style="green")


async def _delete(settings: RickySettings, renderer: CliRenderer, resource: str) -> None:
    _require_interactive(renderer)
    broker, ref = _resource_broker(settings, renderer, resource)
    try:
        await broker.unlock(ref.profile)
        descriptor = await _descriptor(broker, ref)
        if not await _confirm(
            renderer,
            f"Permanently delete current protected ciphertext and approvals for {resource}?",
        ):
            renderer.render_status("Protected-value deletion cancelled.", style="yellow")
            return
        await broker.delete(ref, expected_revision=descriptor.revision)
    finally:
        await broker.aclose()
    renderer.render_status(
        f"Deleted {resource}; encrypted backups may retain historical bytes.", style="green"
    )


async def _policy_show(settings: RickySettings, renderer: CliRenderer, resource: str) -> None:
    broker, ref = _resource_broker(settings, renderer, resource)
    try:
        descriptor = await _descriptor(broker, ref)
    finally:
        await broker.aclose()
    renderer.render_status(descriptor.policy.model_dump_json(indent=2), style="")


async def _policy_set(
    settings: RickySettings,
    renderer: CliRenderer,
    resource: str,
    raw_mode: str,
    origins: list[str],
    *,
    allow_unattended: bool,
    max_unattended_materializations: int,
    allow_unattended_commit: bool,
    max_unattended_commits: int,
    max_unattended_amount_minor: int,
    unattended_currency: str | None,
) -> None:
    _require_interactive(renderer)
    broker, ref = _resource_broker(settings, renderer, resource)
    try:
        await broker.unlock(ref.profile)
        descriptor = await _descriptor(broker, ref)
        policy = descriptor.policy.model_copy(
            update={
                "mode": _destination_mode(raw_mode),
                "authored_origins": tuple(origins),
                "unattended_allowed": allow_unattended,
                "max_unattended_materializations_per_execution": (max_unattended_materializations),
                "unattended_commit_allowed": allow_unattended_commit,
                "max_unattended_commits_per_execution": max_unattended_commits,
                "max_unattended_amount_minor": max_unattended_amount_minor,
                "unattended_currency": unattended_currency,
            }
        )
        policy = ProtectedDestinationPolicy.model_validate(policy.model_dump(), strict=True)
        if not await _confirm(renderer, f"Replace destination policy for {resource}?"):
            renderer.render_status("Protected-value policy update cancelled.", style="yellow")
            return
        updated = await broker.revise(descriptor, policy=policy)
    finally:
        await broker.aclose()
    renderer.render_status(f"Updated policy at revision {updated.revision}.", style="green")


async def _approvals_list(settings: RickySettings, renderer: CliRenderer, resource: str) -> None:
    broker, ref = _resource_broker(settings, renderer, resource)
    try:
        values = await broker.approvals(ref)
    finally:
        await broker.aclose()
    renderer.render_status(
        "\n".join(
            f"{item.top_level_origin} -> {item.frame_origin}  {item.approved_at.isoformat()}"
            for item in values
        )
        or "No durable destination approvals.",
        style="",
    )


async def _approvals_approve(
    settings: RickySettings,
    renderer: CliRenderer,
    resource: str,
    top_origin: str,
    frame_origin: str,
) -> None:
    _require_interactive(renderer)
    if not await _confirm(
        renderer, f"Approve exact destination pair {top_origin} -> {frame_origin} for {resource}?"
    ):
        renderer.render_status("Protected-value approval cancelled.", style="yellow")
        return
    broker, ref = _resource_broker(settings, renderer, resource)
    try:
        approved = await broker.approve(ref, top_level_origin=top_origin, frame_origin=frame_origin)
    finally:
        await broker.aclose()
    renderer.render_status(
        f"Approved {approved.top_level_origin} -> {approved.frame_origin}.", style="green"
    )


async def _approvals_revoke(
    settings: RickySettings,
    renderer: CliRenderer,
    resource: str,
    top_origin: str,
    frame_origin: str,
) -> None:
    _require_interactive(renderer)
    if not await _confirm(
        renderer, f"Revoke exact destination pair {top_origin} -> {frame_origin} for {resource}?"
    ):
        renderer.render_status("Protected-value revocation cancelled.", style="yellow")
        return
    broker, ref = _resource_broker(settings, renderer, resource)
    try:
        revoked = await broker.revoke_approval(
            ref, top_level_origin=top_origin, frame_origin=frame_origin
        )
    finally:
        await broker.aclose()
    renderer.render_status(
        "Revoked exact destination approval." if revoked else "Approval was not present.",
        style="green" if revoked else "yellow",
    )


def _resource_broker(
    settings: RickySettings, renderer: CliRenderer, resource: str
) -> tuple[ProtectedValueBroker, ProfileResourceRef]:
    ref = ProfileResourceRef.from_qualified(resource)
    return _broker(settings, ProfileScope.create(ref.profile), renderer), ref


async def _descriptor(broker: ProtectedValueBroker, ref: ProfileResourceRef):
    values = await broker.catalog(ref=ref)
    if not values:
        raise ProtectedValueStoreError(f"protected value not found: {ref.qualified}")
    return values[0]


async def _secret(renderer: CliRenderer, prompt: str) -> SecretStr:
    value = await renderer.read_secret(prompt)
    if value is None:
        raise ValueError("secure input was cancelled")
    return value


async def _read_values(
    renderer: CliRenderer,
    ref: ProfileResourceRef,
    fields: tuple[ProtectedFieldDescriptor, ...],
) -> dict[str, SecretStr]:
    values: dict[str, SecretStr] = {}
    for field in fields:
        if field.mode == "stored":
            values[field.name] = await _secret(
                renderer, f"Enter {field.label} for {ref.qualified}: "
            )
    return values


async def _confirm(renderer: CliRenderer, prompt: str) -> bool:
    answer = (await renderer.read_line(f"{prompt} [y/N]: ")).strip().lower()
    return answer in {"y", "yes"}


def _require_interactive(renderer: CliRenderer) -> None:
    if not renderer.input_session.interactive:
        raise ValueError("protected-value mutation requires an interactive terminal")


def _kind(value: str) -> ProtectedValueKind:
    allowed = {"credential", "payment_card", "one_time", "generic"}
    if value not in allowed:
        raise ValueError("protected-value kind is invalid")
    return cast(ProtectedValueKind, value)


def _destination_mode(value: str) -> DestinationPolicyMode:
    allowed = {"strict", "confirm_new", "approved_only", "secure_web"}
    if value not in allowed:
        raise ValueError("protected destination mode is invalid")
    return cast(DestinationPolicyMode, value)


def _parse_field(value: str) -> ProtectedFieldDescriptor:
    name, separator, rest = value.partition(":")
    control, separator2, rest = rest.partition(":")
    mode, separator3, label = rest.partition(":")
    if not separator or not separator2 or not separator3:
        raise ValueError("fields must use name:control:mode:label")
    controls = {
        "username",
        "password",
        "one_time_code",
        "cardholder_name",
        "card_number",
        "card_expiry_month",
        "card_expiry_year",
        "card_expiry",
        "card_security_code",
        "generic_secret",
    }
    if control not in controls or mode not in {"stored", "prompt_each_use"}:
        raise ValueError("protected field control or materialization mode is invalid")
    if control == "one_time_code" and mode != "prompt_each_use":
        raise ValueError("one-time-code fields must prompt on every use")
    return ProtectedFieldDescriptor(
        name=name,
        label=label,
        mode=cast(MaterializationMode, mode),
        compatible_controls=(cast(ProtectedControlKind, control),),
    )


def _default_fields(kind: ProtectedValueKind) -> tuple[ProtectedFieldDescriptor, ...]:
    specs: dict[ProtectedValueKind, tuple[tuple[str, str, str, str], ...]] = {
        "credential": (
            ("username", "username", "stored", "Username"),
            ("password", "password", "stored", "Password"),
        ),
        "payment_card": (
            ("cardholder_name", "cardholder_name", "stored", "Cardholder name"),
            ("card_number", "card_number", "stored", "Card number"),
            ("card_expiry", "card_expiry", "stored", "Card expiry"),
            (
                "card_security_code",
                "card_security_code",
                "prompt_each_use",
                "Card security code",
            ),
        ),
        "one_time": (("one_time_code", "one_time_code", "prompt_each_use", "One-time code"),),
        "generic": (("value", "generic_secret", "stored", "Protected value"),),
    }
    return tuple(
        ProtectedFieldDescriptor(
            name=name,
            compatible_controls=(cast(ProtectedControlKind, control),),
            mode=cast(MaterializationMode, mode),
            label=label,
        )
        for name, control, mode, label in specs[kind]
    )
