"""CLI runtime: protected vault password, Gmail challenge, and verified login."""

import json
import re
import shutil
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from playwright.async_api import BrowserType
from pydantic import SecretStr

from ricky.agent import AgentSession
from ricky.authority.registry import AuthorityRegistry
from ricky.browser.authority import browser_authority_evaluators
from ricky.browser.guardrails import browser_guardrail_evaluators
from ricky.browser.playwright_backend import PlaywrightBrowserBackend
from ricky.browser.policy import DestinationPolicy
from ricky.browser.service import BrowserService
from ricky.browser.verification import VerificationMessage
from ricky.config import GoogleAccountSettings, GoogleOAuthClientSettings, GoogleSettings
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.gateway.conversations import ConversationCoordinator
from ricky.gateway.store import GatewayStore
from ricky.llm import MessageDone
from ricky.notifications.routes import RoutePolicy
from ricky.notifications.service import NotificationService
from ricky.permissions import PermissionResponse
from ricky.protected_values import ProtectedValueBroker
from ricky.protected_values.resident import ResidentProtectedValueRegistry
from ricky.runtime import build_session_runtime
from test_browser_gateway_checkout import CheckoutDelegator, checkout_settings
from test_gateway_conversations import (
    _SCOPE,
    HandoffTransport,
    _answer,
    _handoff_messaging,
    _ingest,
    _tool,
)
from test_protected_values_runtime import SENTINEL, _initialize

pytestmark = pytest.mark.browser_integration
ORIGIN = "https://login.example"


class LoginWorker:
    name = "openrouter"

    def __init__(self):
        self.step = 0
        self.session = ""
        self.target = {}

    async def aclose(self):
        pass

    async def stream(self, request):
        self.step += 1
        results = [p for m in request.messages for p in m.content if p.kind == "tool_result"]
        if results:
            assert not results[-1].is_error, results[-1].content
            assert SENTINEL not in results[-1].content
        latest = results[-1].content if results else ""
        if self.step == 1:
            yield _tool("open", "browser_session_open_resource", {"resource": "personal/checkout"})
        elif self.step == 2:
            match = re.search(r"browser_session_[0-9a-f]{32}", latest)
            assert match
            self.session = match.group()
            yield _tool(
                "navigate",
                "browser_navigate",
                {"session_id": self.session, "page_id": None, "url": ORIGIN},
            )
        elif self.step in {3, 5, 7, 10}:
            yield _tool("observe", "browser_snapshot", {"session_id": self.session})
        elif self.step in {4, 6, 8}:
            controls = [
                json.loads(line)
                for line in latest.splitlines()
                if line.startswith("{") and '"control_kind"' in line
            ]
            control = next(
                c
                for c in controls
                if (
                    c.get("protected_kind") == "password"
                    if self.step == 4
                    else c.get("name") == "Sign in"
                    if self.step == 6
                    else c.get("protected_kind") == "one_time_code"
                )
            )
            page = re.search(r"browser_page_[0-9a-f]{32}", latest)
            snapshot = re.search(r"browser_snapshot_[0-9a-f]{32}", latest)
            assert page and snapshot
            self.target = {
                "session_id": self.session,
                "page_id": page.group(),
                "snapshot_id": snapshot.group(),
                "ref": control["ref"],
            }
            if self.step == 4:
                yield _tool(
                    "password",
                    "browser_fill_protected",
                    {
                        "target": self.target,
                        "protected_value": "personal/runtime-login",
                        "field": "password",
                    },
                )
            elif self.step == 6:
                yield _tool(
                    "login",
                    "browser_commit",
                    {"target": self.target, "activation": "click", "envelope": self.envelope()},
                )
            else:
                yield _tool(
                    "challenge",
                    "browser_request_challenge",
                    {
                        "target": self.target,
                        "purpose": "authentication",
                        "instruction": "Enter the email verification code.",
                        "recipient": "owner@example.com",
                    },
                )
        elif self.step == 9:
            challenge = re.search(r"browser_challenge_[0-9a-f]{32}", latest)
            assert challenge
            yield _tool(
                "verify",
                "browser_commit",
                {
                    "target": self.target,
                    "activation": "challenge",
                    "challenge_id": challenge.group(),
                    "envelope": self.envelope(),
                },
            )
        elif self.step == 11 and any(t.name == "report_task_outcome" for t in request.tools):
            assert "Fixture Reader" in latest
            yield _tool(
                "outcome",
                "report_task_outcome",
                {
                    "status": "completed",
                    "summary": "Signed in as Fixture Reader.",
                    "evidence": ["The authenticated page identifies Fixture Reader."],
                },
            )
        else:
            if self.step == 11:
                assert "Fixture Reader" in latest
            yield _answer("Signed in as Fixture Reader.")

    @staticmethod
    def envelope():
        return {
            "kind": "browser",
            "destination": ORIGIN,
            "disclosures": ["Account authentication credentials"],
            "intent": "Sign in to the fixture account",
            "consequences": ["Create an authenticated account session"],
            "expected_result": "Signed in as Fixture Reader",
        }


async def test_cli_vault_password_then_automatic_email_otp(tmp_path, monkeypatch):
    await _login_case(tmp_path, monkeypatch, gateway=False)


async def test_gateway_vault_password_then_automatic_email_otp(tmp_path, monkeypatch):
    await _login_case(tmp_path, monkeypatch, gateway=True)


async def _login_case(tmp_path, monkeypatch, *, gateway):
    settings = checkout_settings(tmp_path)
    settings.protected_values.enabled = True
    settings.protected_values.argon2_iterations = 1
    settings.protected_values.argon2_memory_kib = 8192
    settings.protected_values.argon2_lanes = 1
    await _initialize(settings, origin=ORIGIN)
    if gateway:
        broker = ProtectedValueBroker(settings, scope=settings.resolve_profile_scope("personal"))
        try:
            await broker.unlock("personal", SecretStr("runtime-passphrase"))
            descriptor = (await broker.catalog())[0]
            await broker.revise(
                descriptor,
                policy=descriptor.policy.model_copy(
                    update={
                        "unattended_allowed": True,
                        "max_unattended_materializations_per_execution": 1,
                        "unattended_commit_allowed": True,
                        "max_unattended_commits_per_execution": 2,
                    }
                ),
            )
        finally:
            await broker.aclose()
    profile = settings.profile_configs["personal"]
    profile.google = GoogleSettings(
        accounts={"mail": GoogleAccountSettings(email="owner@example.com")}
    )
    profile.google_oauth_clients = {
        "mail": GoogleOAuthClientSettings(client_id="fixture", client_secret=SecretStr("fixture"))
    }
    settings.browser.verification.enabled = True
    settings.browser.verification.gmail_accounts = ["personal/mail"]
    settings.browser.verification.allow_background = gateway
    executable = shutil.which("google-chrome-stable")
    assert executable
    submitted = []
    launch = BrowserType.launch_persistent_context

    async def fixture_launch(self, *args, **kwargs):
        context = await launch(self, *args, **kwargs)

        async def respond(route):
            request = route.request
            if not request.url.startswith(ORIGIN):
                await route.abort()
                return
            if request.method == "POST" and request.url.endswith("/verify"):
                assert parse_qs(request.post_data or "").get("otp") == ["123456"]
                submitted.append("verified")
                body = "<h1>Signed in</h1><p>Fixture Reader</p>"
            elif request.method == "POST":
                assert parse_qs(request.post_data or "").get("password") == [SENTINEL]
                submitted.append("password")
                body = """<form method="post" action="/verify"><label>Email verification code
<input name="otp" autocomplete="one-time-code"
oninput="if(this.value.length===6)this.form.requestSubmit()"></label></form>"""
            else:
                body = """<form method="post" action="/login"><label>Account password
<input type="password" name="password" autocomplete="current-password"></label>
<button type="submit">Sign in</button></form>"""
            await route.fulfill(status=200, content_type="text/html", body=body)

        await context.route("**/*", respond)
        return context

    async def resolve(_host, _port):
        return ("93.184.216.34",)

    async def read_email(_reader, query):
        assert submitted == ["password"]
        assert query.source.account.qualified == "personal/mail"
        return (
            VerificationMessage(
                account=query.source.account,
                message_id="login-code",
                received_at=datetime.now(UTC),
                sender="verify@login.example",
                recipients=("owner@example.com",),
                subject="Login verification",
                text="Your verification code is 123456.",
            ),
        )

    monkeypatch.setattr(BrowserType, "launch_persistent_context", fixture_launch)
    monkeypatch.setattr(
        "ricky.browser.service.DestinationPolicy",
        lambda **kw: DestinationPolicy(resolver=resolve, **kw),
    )
    monkeypatch.setattr(
        "ricky.tools.integrations.gmail.verification.GmailVerificationReader.read", read_email
    )

    async def browser_factory(config, **kwargs):
        return BrowserService(
            config, backend=PlaywrightBrowserBackend(), executable_path=Path(executable), **kwargs
        )

    reviews = []

    async def approve(event):
        reviews.append(event.tool_name)
        return PermissionResponse(decision="allow")

    async def user_code(_owner):
        pytest.fail("an authorized email code must not require user input")

    if gateway:
        await _gateway_login(settings, tmp_path, monkeypatch, browser_factory)
        assert submitted == ["password", "verified"]
        return

    scope = settings.resolve_profile_scope("personal")
    session = AgentSession.create(
        settings, profile_scope=scope, provider="openrouter", model="test"
    )
    async with build_session_runtime(
        settings,
        session=session,
        provider=LoginWorker(),
        browser_factory=browser_factory,
        permission_responder=approve,
        browser_challenge_responder=user_code,
        project_root=tmp_path,
    ) as runtime:
        broker = runtime.capabilities.protected_values
        assert broker is not None
        await broker.unlock("personal", SecretStr("runtime-passphrase"))
        async for event in runtime.agent_loop.run_turn(
            session,
            "Open https://login.example in personal/checkout. Sign in with "
            "personal/runtime-login, complete email verification, and report the account name.",
        ):
            assert event.kind != "agent_error"
    assert submitted == ["password", "verified"]
    assert reviews.count("browser_commit") == 2
    assert SENTINEL not in session.model_dump_json()
    assert any(
        "Fixture Reader" in p.text
        for m in session.history
        if m.role == "assistant"
        for p in m.content
        if p.kind == "text"
    )


async def _gateway_login(settings, tmp_path, monkeypatch, browser_factory):
    settings.browser.background.budget.transaction_commits = 2
    settings.browser.background.protected_values_enabled = True
    settings.authority.capabilities["protected_value_use"] = settings.authority.capabilities[
        "browser_interact"
    ].model_copy()
    monkeypatch.setattr(
        "ricky.runtime.composition.built_in_guardrail_evaluators", browser_guardrail_evaluators
    )
    monkeypatch.setattr(
        "ricky.authority.compiler.default_authority_registry",
        lambda: AuthorityRegistry(list(browser_authority_evaluators())),
    )

    class Delegator(CheckoutDelegator):
        async def stream(self, request):
            async for event in super().stream(request):
                if isinstance(event, MessageDone):
                    for part in event.message.content:
                        if part.kind == "tool_call" and part.name == "delegate_task":
                            part.args["goal"] = (
                                "Sign in at https://login.example with personal/runtime-login, "
                                "verify email and report account name."
                            )
                            part.args["requested_capabilities"].append(
                                "builtin.protected_value.use"
                            )
                            for guard in part.args["guardrails"]:
                                for field in guard["fields"]:
                                    if field["field"] == "authenticated_origins":
                                        field["value"] = "personal/checkout#" + ORIGIN
                                    if (
                                        field["field"] == "allowed_tools"
                                        and guard["capability_id"] == "builtin.browser.interact"
                                    ):
                                        field["value"] += ",browser_request_challenge"
                            part.args["guardrails"].append(
                                {
                                    "capability_id": "builtin.protected_value.use",
                                    "fields": [
                                        {"field": "mode", "value": "transaction"},
                                        {
                                            "field": "allowed_tools",
                                            "value": "browser_fill_protected",
                                        },
                                        {"field": "resources", "value": "personal/checkout"},
                                        {
                                            "field": "authenticated_origins",
                                            "value": "personal/checkout#" + ORIGIN,
                                        },
                                        {
                                            "field": "protected_values",
                                            "value": "personal/runtime-login#password",
                                        },
                                    ],
                                }
                            )
                yield event

    @asynccontextmanager
    async def runtime_factory(*args, **kwargs):
        async with build_session_runtime(
            *args, background_browser_factory=browser_factory, **kwargs
        ) as runtime:
            yield runtime

    monkeypatch.setattr("ricky.jobs.runner.build_session_runtime", runtime_factory)
    registry = ResidentProtectedValueRegistry(settings)
    try:
        await registry.unlock("personal", SecretStr("runtime-passphrase"))
        foreground = Delegator()
        coordinator = ConversationCoordinator(
            settings, provider_factory=lambda *_: foreground, protected_value_registry=registry
        )
        message = await _ingest(
            settings, suffix="a", text="Sign in and complete email verification."
        )
        await coordinator.process(message.id)
        transport = HandoffTransport()
        messaging = _handoff_messaging(settings, transport)
        assert await messaging.deliver_once() == 1
        assert await coordinator.reconcile_handoffs() == 1
        routes = RoutePolicy(settings, conversation_resolver=GatewayStore(settings))
        dispatcher = ExecutionDispatcher(
            settings,
            project_root=tmp_path,
            provider_factory=lambda _: LoginWorker(),
            authority_registry=AuthorityRegistry(list(browser_authority_evaluators())),
            routes=routes,
            notifications=NotificationService(settings, routes=routes),
            protected_value_registry=registry,
        )
        coordinator.bind_dispatcher(dispatcher)
        approvals = []
        notify = dispatcher.notify_browser_approval

        async def approve(challenge, *, scope):
            await notify(challenge, scope=scope)
            await messaging.deliver_once()
            command = re.search(r"/approve browser_\S+ \S+", transport.sent[-1].text)
            assert command
            approvals.append(challenge.approval.id)
            reply = await _ingest(
                settings, suffix="b" if len(approvals) == 1 else "c", text=command.group()
            )
            await coordinator.process(reply.id)

        async def no_user_code(*args):
            pytest.fail("gateway must retrieve this authorized email code without user help")

        monkeypatch.setattr(dispatcher, "notify_browser_approval", approve)
        monkeypatch.setattr(dispatcher, "_request_browser_challenge", no_user_code)
        completed = await dispatcher.worker_once(scope=_SCOPE)
        assert len(completed) == 1 and completed[0].status == "succeeded", [
            (r.status, r.error) for r in completed
        ]
        assert len(approvals) == 2
        assert all(SENTINEL not in sent.text for sent in transport.sent)
    finally:
        await registry.aclose()
