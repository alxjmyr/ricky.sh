"""CLI composition root, configuration commands, and interactive entry points."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer

from ricky import __version__
from ricky.agent import AgentSession
from ricky.browser import BrowserService
from ricky.config import (
    RickySettings,
    config_file,
    load_settings,
    profile_config_file,
    profile_data_path,
    profile_secrets_file,
    user_data_path,
)
from ricky.installation import (
    InstallationError,
    bootstrap_file,
    installation_operation_lock,
    require_compatible_installation,
)
from ricky.interfaces.cli.browser import register_browser_commands
from ricky.interfaces.cli.capabilities import register_capability_commands
from ricky.interfaces.cli.chat import ChatController
from ricky.interfaces.cli.errors import run_with_provider_errors
from ricky.interfaces.cli.executions import (
    register_authority_commands,
    register_execution_commands,
    register_execution_contract_commands,
    register_execution_draft_commands,
)
from ricky.interfaces.cli.gateway import register_gateway_commands
from ricky.interfaces.cli.installation import register_installation_commands
from ricky.interfaces.cli.jobs import (
    register_job_action_commands,
    register_job_commands,
    register_schedule_commands,
)
from ricky.interfaces.cli.notifications import register_notification_commands
from ricky.interfaces.cli.profiles import register_profile_commands
from ricky.interfaces.cli.protected_values import register_protected_value_commands
from ricky.interfaces.cli.render import CliRenderer
from ricky.interfaces.cli.select import run_model_picker
from ricky.interfaces.cli.sessions import register_session_commands
from ricky.interfaces.cli.tasks import register_task_commands
from ricky.interfaces.cli.workflows import register_workflow_commands
from ricky.llm import CompletionRequest, Message, MessageDone, TextDelta, create_provider
from ricky.memory import memory_note_counts
from ricky.profiles import ProfileResourceRef
from ricky.runtime import build_session_runtime
from ricky.skills.registry import discover_skills
from ricky.tools import ToolRegistry
from ricky.tools.integrations.gcal import GcalError, gcal_toolset
from ricky.tools.integrations.gmail import GmailError, gmail_toolset
from ricky.tools.integrations.google import (
    ALL_SERVICE_SCOPES,
    SERVICE_SCOPES,
    GoogleAuth,
    GoogleAuthError,
)
from ricky.tools.integrations.slack import SlackError, slack_toolset
from ricky.tools.integrations.web_search import web_search_toolset

app = typer.Typer(
    name="ricky",
    help="A personal agentic assistant and agent harness.",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)
config_app = typer.Typer(
    help="Inspect or update ricky configuration.",
    invoke_without_command=True,
    add_completion=False,
)
app.add_typer(config_app, name="config")
google_config_app = typer.Typer(
    help="Inspect or authorize named Google accounts.",
    invoke_without_command=True,
    add_completion=False,
)
config_app.add_typer(google_config_app, name="google")
workflow_app = typer.Typer(
    help="Inspect, validate, and dry-run workflows.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(workflow_app, name="workflow")
register_workflow_commands(workflow_app)
task_app = typer.Typer(
    help="Inspect and update cross-session durable tasks.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(task_app, name="task")
register_task_commands(task_app)
job_app = typer.Typer(
    help="Run and inspect bounded read-only agent jobs.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(job_app, name="job")
register_job_commands(job_app)
job_action_app = typer.Typer(
    help="Inspect and explicitly reconcile guarded external job actions.",
    no_args_is_help=True,
    add_completion=False,
)
job_app.add_typer(job_action_app, name="action")
register_job_action_commands(job_action_app)
schedule_app = typer.Typer(
    help="Manage verified cron schedules for named jobs.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(schedule_app, name="schedule")
register_schedule_commands(schedule_app)
session_app = typer.Typer(
    help="Inspect, archive, and resume persistent conversations.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(session_app, name="session")
register_session_commands(session_app)
gateway_app = typer.Typer(
    help="Operate persistent messaging transports and the durable inbox.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(gateway_app, name="gateway")
register_gateway_commands(gateway_app)
capability_app = typer.Typer(
    help="Inspect installed capability expansion and standing agent policy.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(capability_app, name="capability")
register_capability_commands(capability_app)
browser_app = typer.Typer(
    help="Install Chromium and manage browser resources.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(browser_app, name="browser")
register_browser_commands(browser_app)
protected_values_app = typer.Typer(
    help="Manage profile-scoped encrypted protected values.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(protected_values_app, name="protected-values")
register_protected_value_commands(protected_values_app)
register_installation_commands(app)
register_profile_commands(app)
notification_app = typer.Typer(
    help="Inspect and reconcile durable user notifications.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(notification_app, name="notification")
register_notification_commands(notification_app)
execution_app = typer.Typer(
    help="Inspect and run durable fire-and-report execution requests.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(execution_app, name="execution")
register_execution_commands(execution_app)
execution_draft_app = typer.Typer(
    help="Inspect and cancel durable live execution-review drafts.",
    no_args_is_help=True,
    add_completion=False,
)
execution_app.add_typer(execution_draft_app, name="draft")
register_execution_draft_commands(execution_draft_app)
execution_contract_app = typer.Typer(
    help="Inspect immutable capability-compiled execution contracts.",
    no_args_is_help=True,
    add_completion=False,
)
execution_app.add_typer(execution_contract_app, name="contract")
register_execution_contract_commands(execution_contract_app)
authority_app = typer.Typer(
    help="Inspect and revoke task-scoped delegated authority.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(authority_app, name="authority")
register_authority_commands(authority_app)


def _version_callback(value: bool) -> None:
    if value:
        CliRenderer().render_status(f"ricky {__version__}", style="")
        raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the ricky version and exit.",
    ),
) -> None:
    """ricky - a personal agentic assistant and agent harness."""
    # Lifecycle commands own their exclusive/recovery locking. Every ordinary
    # command against an initialized release holds shared authority until its
    # Click context closes, before configuration or durable state is opened.
    if ctx.invoked_subcommand not in {
        "init",
        "upgrade",
        "_upgrade-handoff",
        "decommission",
        "data",
        "profile",
    }:
        try:
            pointer_path = bootstrap_file()
            if pointer_path.exists() or pointer_path.is_symlink():
                ctx.with_resource(
                    installation_operation_lock(
                        mode="shared",
                        timeout_seconds=5.0,
                        operation="runtime",
                    )
                )
                require_compatible_installation()
        except (InstallationError, OSError) as exc:
            CliRenderer().render_error(f"Installation error: {exc}")
            raise typer.Exit(1) from exc
    if ctx.invoked_subcommand is None:
        run_with_provider_errors(lambda renderer: _chat(None, None, renderer))


@config_app.callback()
def config_callback(ctx: typer.Context) -> None:
    """Show the resolved configuration (secrets redacted)."""
    if ctx.invoked_subcommand is None:
        root_settings = load_settings()
        scope = root_settings.resolve_profile_scope()
        settings = root_settings.resolve_profile_runtime_settings(scope)
        root = user_data_path(root_settings)
        CliRenderer().render_config(
            settings,
            installation_config_path=config_file(root),
            profile_config_path=profile_config_file(scope.primary, root),
            profile_secrets_path=profile_secrets_file(scope.primary, root),
        )


@config_app.command("model")
def config_model(
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Profile whose provider and model defaults will be updated.",
    ),
) -> None:
    """Interactively choose and persist one profile's provider and model defaults."""
    run_with_provider_errors(lambda renderer: _config_model(profile, renderer))


async def _config_model(profile: str | None, renderer: CliRenderer) -> None:
    root_settings = load_settings()
    scope = root_settings.resolve_profile_scope(profile)
    await run_model_picker(root_settings, renderer, profile_scope=scope, profile=scope.primary)


@google_config_app.callback()
def config_google_callback(ctx: typer.Context) -> None:
    """Show redacted OAuth status for every configured Google account."""
    if ctx.invoked_subcommand is None:
        run_with_provider_errors(_check_google)


@google_config_app.command("auth")
def config_google_auth(
    account: str = typer.Argument(..., help="Configured Google account name."),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Print the consent URL without attempting to open a browser.",
    ),
    callback_port: int | None = typer.Option(
        None,
        "--callback-port",
        min=1,
        max=65535,
        help="Fixed loopback callback port; use with SSH port forwarding.",
    ),
) -> None:
    """Authorize one Google account via PKCE consent."""
    run_with_provider_errors(
        lambda renderer: _authorize_google(
            account,
            renderer,
            open_browser=not no_browser,
            callback_port=callback_port or 0,
        )
    )


@config_app.command("gmail")
def config_gmail() -> None:
    """Check Gmail identity and mailbox totals for every configured account."""
    run_with_provider_errors(_check_gmail)


@config_app.command("gcal")
def config_gcal() -> None:
    """Check Calendar identity, primary calendar, and timezone for each account."""
    run_with_provider_errors(_check_gcal)


@config_app.command("slack")
def config_slack() -> None:
    """Check Slack authentication for every configured profile."""
    run_with_provider_errors(_check_slack)


@config_app.command("memory")
def config_memory() -> None:
    """Show profile-owned memory roots and read-only note counts."""
    settings = load_settings()
    CliRenderer().render_memory_config(
        roots={
            profile: profile_data_path(settings, profile) / "memory"
            for profile in settings.profiles.enabled
        },
        counts=memory_note_counts(settings),
    )


async def _check_google(renderer: CliRenderer) -> None:
    root_settings = load_settings()
    settings = root_settings.resolve_profile_runtime_settings(
        root_settings.resolve_profile_scope(
            access_profiles=root_settings.profiles.enabled,
        )
    )
    auth = GoogleAuth(settings, scopes=ALL_SERVICE_SCOPES)
    failed = False
    try:
        if not auth.account_names:
            renderer.render_status(
                "No Google accounts are configured in an enabled profile.",
                style="yellow",
            )
            raise typer.Exit(1)
        for status in auth.statuses():
            prefix = f"Google [{status.account}] {status.expected_email}:"
            if not status.client_configured:
                failed = True
                profile_name, account_name = status.account.split("/", 1)
                resource = ProfileResourceRef(profile=profile_name, name=account_name)
                renderer.render_status(
                    f"{prefix} OAuth client missing; add "
                    f"[google_oauth_clients.{resource.name}] to "
                    f"<user_data_dir>/profiles/{resource.profile}/.secrets.toml.",
                    style="yellow",
                )
            elif not status.token_present:
                failed = True
                renderer.render_status(
                    f"{prefix} not authorized; run ricky config google auth {status.account}.",
                    style="yellow",
                )
            elif status.client_matches is False:
                failed = True
                renderer.render_status(
                    f"{prefix} OAuth client changed; "
                    f"run ricky config google auth {status.account}.",
                    style="yellow",
                )
            elif status.missing_scopes:
                # Granular consent may grant a subset of services; only a
                # token with no usable service scope is a failure.
                enabled, disabled = _service_readiness(status.granted_scopes)
                if not enabled:
                    failed = True
                    renderer.render_status(
                        f"{prefix} missing scopes {', '.join(status.missing_scopes)}; "
                        f"run ricky config google auth {status.account}.",
                        style="yellow",
                    )
                else:
                    renderer.render_status(
                        f"{prefix} authorized as {status.stored_email}; services: "
                        f"{', '.join(enabled)} (not granted: {', '.join(disabled)}; "
                        f"re-run ricky config google auth {status.account} to enable).",
                        style="yellow",
                    )
            else:
                scopes = ", ".join(status.granted_scopes)
                renderer.render_status(
                    f"{prefix} authorized as {status.stored_email}; scopes: {scopes}.",
                    style="green",
                )
    finally:
        await auth.aclose()
    if failed:
        raise typer.Exit(1)


async def _authorize_google(
    account: str,
    renderer: CliRenderer,
    *,
    open_browser: bool,
    callback_port: int,
) -> None:
    root_settings = load_settings()
    settings = root_settings.resolve_profile_runtime_settings(
        root_settings.resolve_profile_scope(
            access_profiles=root_settings.profiles.enabled,
        )
    )
    auth = GoogleAuth(settings, scopes=ALL_SERVICE_SCOPES)

    def show_authorization_url(url: str) -> None:
        behavior = (
            "also opening a browser"
            if open_browser
            else (
                "browser launch disabled; open the URL on your local machine and "
                "forward its callback port over SSH when Ricky is remote"
            )
        )
        renderer.render_status(
            f"Open this URL to authorize [{account}] ({behavior}):\n{url}",
            style="cyan",
        )

    try:
        status = await auth.authorize(
            account,
            on_authorization_url=show_authorization_url,
            open_browser=open_browser,
            callback_port=callback_port,
        )
    finally:
        await auth.aclose()
    enabled, disabled = _service_readiness(status.granted_scopes)
    detail = f"; services enabled: {', '.join(enabled) or '[none]'}"
    if disabled:
        detail += f" (not granted: {', '.join(disabled)})"
    renderer.render_status(
        f"Google [{status.account}] authorized as {status.stored_email}{detail}.",
        style="green",
    )


def _service_readiness(granted_scopes: list[str]) -> tuple[list[str], list[str]]:
    """Split known Google services into (enabled, not granted) for one grant."""
    granted = set(granted_scopes)
    enabled = [name for name, scopes in SERVICE_SCOPES.items() if scopes <= granted]
    disabled = [name for name in SERVICE_SCOPES if name not in enabled]
    return enabled, disabled


async def _check_gmail(renderer: CliRenderer) -> None:
    root_settings = load_settings()
    settings = root_settings.resolve_profile_runtime_settings(
        root_settings.resolve_profile_scope(
            access_profiles=root_settings.profiles.enabled,
        )
    )
    toolset = gmail_toolset(settings)
    if toolset is None:
        _render_missing_google_credentials(renderer)
        raise typer.Exit(1)

    async def probe(account: str) -> str:
        email, total = await toolset.check_account(account)
        return f"Gmail [{account}] OK: authenticated as {email}; {total} total messages."

    await _check_google_accounts(
        renderer,
        settings=settings,
        label="Gmail",
        probe=probe,
        error_types=(GoogleAuthError, GmailError),
        aclose=toolset.aclose,
    )


async def _check_gcal(renderer: CliRenderer) -> None:
    root_settings = load_settings()
    settings = root_settings.resolve_profile_runtime_settings(
        root_settings.resolve_profile_scope(
            access_profiles=root_settings.profiles.enabled,
        )
    )
    toolset = gcal_toolset(settings)
    if toolset is None:
        _render_missing_google_credentials(renderer)
        raise typer.Exit(1)

    async def probe(account: str) -> str:
        primary, timezone = await toolset.check_account(account)
        return f"Calendar [{account}] OK: primary {primary}; timezone {timezone}."

    await _check_google_accounts(
        renderer,
        settings=settings,
        label="Calendar",
        probe=probe,
        error_types=(GoogleAuthError, GcalError),
        aclose=toolset.aclose,
    )


def _render_missing_google_credentials(renderer: CliRenderer) -> None:
    renderer.render_status(
        "No configured Google account has matching OAuth client credentials. "
        "Add [google_oauth_clients.<account>] to the owning profile's .secrets.toml.",
        style="yellow",
    )


async def _check_google_accounts(
    renderer: CliRenderer,
    *,
    settings: RickySettings,
    label: str,
    probe: Callable[[str], Coroutine[Any, Any, str]],
    error_types: tuple[type[Exception], ...],
    aclose: Callable[[], Coroutine[Any, Any, None]],
) -> None:
    """Probe every credentialed account; accounts without OAuth credentials
    are reported as skipped, matching the toolset availability rule."""
    failed = False
    try:
        for account in sorted(settings.google.accounts):
            if account not in settings.google_oauth_clients:
                renderer.render_status(
                    f"{label} [{account}] skipped: no OAuth client configured.",
                    style="yellow",
                )
                continue
            try:
                line = await probe(account)
            except error_types as exc:
                failed = True
                renderer.render_error(f"{label} [{account}] check failed: {exc}")
                continue
            renderer.render_status(line, style="green")
    finally:
        await aclose()
    if failed:
        raise typer.Exit(1)


async def _check_slack(renderer: CliRenderer) -> None:
    root_settings = load_settings()
    configured_profiles = [
        profile
        for profile in root_settings.profiles.enabled
        if (configured := root_settings.profile_configs.get(profile)) is not None
        and (configured.slack is not None or configured.slack_user_token is not None)
    ]
    # A conventional environment credential is installation-level rather than
    # present in one profile document. Check it through the default scope once.
    profiles = configured_profiles or [root_settings.profiles.default]
    failed = False
    found = False
    for profile in profiles:
        settings = root_settings.resolve_profile_runtime_settings(
            root_settings.resolve_profile_scope(profile)
        )
        toolset = slack_toolset(settings)
        if toolset is None:
            failed = True
            renderer.render_status(
                f"Slack [{profile}] slack_user_token is not set. Add it to the owning "
                "profile's .secrets.toml; required user-token scopes are listed in "
                ".designs/assets/slack-app-manifest.yaml.",
                style="yellow",
            )
            continue
        found = True
        try:
            user, team = await toolset.check_auth()
        except SlackError as exc:
            failed = True
            renderer.render_error(f"Slack [{profile}] auth check failed: {exc}")
        else:
            renderer.render_status(
                f"Slack [{profile}] OK: authenticated as {user} in {team}.",
                style="green",
            )
        finally:
            await toolset.aclose()
    if failed or not found:
        raise typer.Exit(1)


@app.command()
def chat(
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help="Override the default provider.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Override the provider's default model.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Primary profile for this session.",
    ),
    access_profiles: Annotated[
        list[str] | None,
        typer.Option("--access-profile", help="Additional accessible profile; repeatable."),
    ] = None,
) -> None:
    """Start an interactive agent chat session."""
    run_with_provider_errors(
        lambda renderer: _chat(
            provider,
            model,
            renderer,
            profile_name=profile,
            access_profiles=access_profiles or (),
        )
    )


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="Prompt to send to the configured model."),
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help="Override the default provider.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Override the provider's default model.",
    ),
    temperature: float | None = typer.Option(None, help="Sampling temperature."),
    max_tokens: int | None = typer.Option(None, help="Maximum completion tokens."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Primary profile for this request.",
    ),
    access_profiles: Annotated[
        list[str] | None,
        typer.Option("--access-profile", help="Additional accessible profile; repeatable."),
    ] = None,
) -> None:
    """Ask the configured model once and stream the reply."""
    run_with_provider_errors(
        lambda renderer: _ask(
            prompt,
            provider,
            model,
            temperature,
            max_tokens,
            renderer,
            profile_name=profile,
            access_profiles=access_profiles or (),
        )
    )


async def _chat(
    provider_name: str | None,
    model: str | None,
    renderer: CliRenderer,
    *,
    profile_name: str | None = None,
    access_profiles: tuple[str, ...] | list[str] = (),
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(
        profile_name,
        access_profiles=access_profiles,
    )
    selection = settings.resolve_profile_selection(profile_scope, provider_name, model)
    provider = create_provider(
        selection.provider,
        settings.resolve_profile_runtime_settings(profile_scope),
    )
    session = AgentSession.create(
        settings,
        profile_scope=profile_scope,
        provider=selection.provider,
        model=selection.model,
    )
    async with build_session_runtime(
        settings,
        session=session,
        provider=provider,
        permission_responder=renderer.request_permission,
        approval_responder=renderer.request_workflow_approval,
        slack_factory=slack_toolset,
        gmail_factory=gmail_toolset,
        gcal_factory=gcal_toolset,
        web_search_factory=web_search_toolset,
        google_auth_factory=GoogleAuth,
        browser_factory=BrowserService.create,
        unlock_responder=renderer.request_protected_unlock,
        secure_value_responder=renderer.request_secure_value,
        destination_responder=renderer.request_protected_destination,
        skill_factory=discover_skills,
        registry_factory=ToolRegistry,
    ) as runtime:
        controller = ChatController(
            agent_loop=runtime.agent_loop,
            session=session,
            settings=settings,
            renderer=renderer,
            skill_registry=runtime.skill_registry,
            memory=runtime.memory,
            durable_tasks=runtime.durable_tasks,
            workflow_runner=runtime.workflow_runner,
            workflow_registry=runtime.workflow_registry,
            session_artifacts=runtime.capabilities.session_artifacts,
            session_media=runtime.capabilities.session_media,
        )
        await controller.run()


async def _ask(
    prompt: str,
    provider_name: str | None,
    model: str | None,
    temperature: float | None,
    max_tokens: int | None,
    renderer: CliRenderer,
    *,
    profile_name: str | None = None,
    access_profiles: tuple[str, ...] | list[str] = (),
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(
        profile_name,
        access_profiles=access_profiles,
    )
    selection = settings.resolve_profile_selection(profile_scope, provider_name, model)
    provider = create_provider(
        selection.provider,
        settings.resolve_profile_runtime_settings(profile_scope),
    )
    request = CompletionRequest(
        model=selection.model,
        messages=[Message.text("user", prompt)],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    try:
        async for event in provider.stream(request):
            if isinstance(event, TextDelta):
                renderer.console.file.write(event.delta)
                renderer.console.file.flush()
            elif isinstance(event, MessageDone):
                renderer.console.file.write("\n")
                renderer.console.file.flush()
    finally:
        await provider.aclose()


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
