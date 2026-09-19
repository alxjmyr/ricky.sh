"""Full gateway checkout, OTP reply, fresh review, and one completed purchase."""

import asyncio
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

from browser_transaction_support import checkout_html
from ricky.authority.registry import AuthorityRegistry
from ricky.authority.store import AuthorityStore
from ricky.browser.authority import browser_authority_evaluators
from ricky.browser.challenge_store import BrowserChallengeStore
from ricky.browser.guardrails import browser_guardrail_evaluators
from ricky.browser.playwright_backend import PlaywrightBrowserBackend
from ricky.browser.policy import DestinationPolicy
from ricky.browser.service import BrowserService
from ricky.browser.verification import VerificationMessage, VerificationUnavailable
from ricky.config import GoogleAccountSettings, GoogleOAuthClientSettings, GoogleSettings
from ricky.executions.contracts import ExecutionContract, build_execution_contract
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.executions.store import ExecutionStore
from ricky.gateway.conversations import ConversationCoordinator
from ricky.gateway.store import GatewayStore
from ricky.llm import MessageDone
from ricky.notifications.routes import RoutePolicy
from ricky.notifications.service import NotificationService
from ricky.runtime import build_session_runtime
from test_browser_gateway_checkout import (
    ORIGIN,
    CheckoutDelegator,
    CheckoutWorker,
    checkout_settings,
)
from test_gateway_conversations import (
    _SCOPE,
    HandoffTransport,
    _answer,
    _handoff_messaging,
    _ingest,
    _tool,
)

pytestmark = pytest.mark.browser_integration


class ChallengeDelegator(CheckoutDelegator):
    async def stream(self, request):
        async for event in super().stream(request):
            if not isinstance(event, MessageDone):
                yield event
                continue
            for part in event.message.content:
                if part.kind == "tool_call" and part.name == "delegate_task":
                    for guard in part.args["guardrails"]:
                        if guard["capability_id"] == "builtin.browser.interact":
                            for field in guard["fields"]:
                                if field["field"] == "allowed_tools":
                                    field["value"] += ",browser_request_challenge"
            yield event


class ChallengeWorker(CheckoutWorker):
    def __init__(self, scenario="code"):
        super().__init__()
        self.scenario = scenario
        self.observation_waits = 0
        self.reported = False
        self.final_submitted = False

    async def stream(self, request):
        if self.step < 9:
            async for event in super().stream(request):
                yield event
            return
        self.step += 1
        results = [
            p.content for m in request.messages for p in m.content if p.kind == "tool_result"
        ]
        if self.step == 10:
            latest = results[-1]
            controls = [
                json.loads(line)
                for line in latest.splitlines()
                if line.startswith("{") and '"control_kind"' in line
            ]
            control = next(c for c in controls if c.get("protected_kind") == "one_time_code")
            session = re.search(r"browser_session_[0-9a-f]{32}", latest)
            page = re.search(r"browser_page_[0-9a-f]{32}", latest)
            snapshot = re.search(r"browser_snapshot_[0-9a-f]{32}", latest)
            assert session and page and snapshot
            self.target = {
                "session_id": session.group(),
                "page_id": page.group(),
                "snapshot_id": snapshot.group(),
                "ref": control["ref"],
            }
            yield _tool(
                "challenge",
                "browser_request_challenge",
                {
                    "target": self.target,
                    "instruction": "Enter the emailed code.",
                    "purpose": "transaction",
                },
            )
        elif self.step == 11:
            if "eligible untrusted email" in results[-1]:
                candidates = json.loads(results[-1].split("\n", 1)[1])
                candidate = candidates[0]
                token = next(
                    (item for item in candidate["tokens"] if item["text"] == "AB12CD"), None
                )
                challenge = re.search(r"browser_challenge_[0-9a-f]{32}", results[-1])
                assert challenge is not None
                self.step = 10
                yield _tool(
                    "interpret",
                    "browser_request_challenge",
                    {
                        "target": self.target,
                        "purpose": "transaction",
                        "instruction": "Verify the email code.",
                        "answer": {
                            "challenge_id": challenge.group(),
                            "message_id": candidate["message"]["message_id"],
                            "token_index": token["index"] if token else None,
                        },
                    },
                )
                return
            challenge_id = re.search(r"browser_challenge_[0-9a-f]{32}", results[-1])
            assert challenge_id is not None, results[-1]
            envelope = {
                "kind": "financial",
                "intent": "Verify the pending credit purchase",
                "payee": "Fixture merchant",
                "total": {"amount": "21.60", "currency": "USD"},
                "components": [
                    {"label": "Credits", "amount": {"amount": "20.00", "currency": "USD"}}
                ],
                "fees": [{"label": "Service fee", "amount": {"amount": "1.60", "currency": "USD"}}],
                "timing": "one_time",
                "source": {"kind": "site", "label": "Saved account"},
                "consequences": ["Finalize the pending $21.60 purchase"],
                "expected_result": "Credit balance $26.42",
            }
            self.envelope = envelope
            yield _tool(
                "verify",
                "browser_commit",
                {
                    "target": self.target,
                    "activation": "challenge",
                    "challenge_id": challenge_id.group(),
                    "envelope": envelope,
                },
            )
        elif self.step == 12:
            yield _tool("observe", "browser_snapshot", {"session_id": self.target["session_id"]})
        else:
            if self.reported:
                yield _answer("Task outcome reported with the observed account state.")
                return
            if self.scenario == "email_continue" and not self.final_submitted:
                latest = results[-1]
                assert "Verification accepted" in latest
                control = next(
                    json.loads(line)
                    for line in latest.splitlines()
                    if line.startswith("{") and '"name": "Complete purchase"' in line
                )
                snapshot = re.search(r"browser_snapshot_[0-9a-f]{32}", latest)
                assert snapshot is not None
                self.final_submitted = True
                yield _tool(
                    "complete-purchase",
                    "browser_commit",
                    {
                        "target": {
                            **self.target,
                            "snapshot_id": snapshot.group(),
                            "ref": control["ref"],
                        },
                        "envelope": self.envelope,
                    },
                )
                return
            if "Processing verification" in results[-1] and self.observation_waits < 2:
                self.observation_waits += 1
                yield _tool(
                    f"wait-{self.observation_waits}",
                    "browser_snapshot",
                    {"session_id": self.target["session_id"], "wait_seconds": 4.0},
                )
                return
            uncertain = self.scenario == "email_unconfirmed"
            blocked = self.scenario == "email_rejected"
            if blocked:
                assert "Verification rejected; no purchase was made" in results[-1]
            if not uncertain and not blocked:
                assert "26.42" in results[-1], results[-1]
            self.reported = True
            yield _tool(
                "outcome",
                "report_task_outcome",
                {
                    "status": "uncertain" if uncertain else "blocked" if blocked else "completed",
                    "summary": "Purchase remains unconfirmed."
                    if uncertain
                    else "Verification rejected; no purchase was made."
                    if blocked
                    else "Purchase completed.",
                    "evidence": [
                        "Processing verification remained visible after bounded waits."
                        if uncertain
                        else "The site explicitly rejected verification without purchasing."
                        if blocked
                        else "Credit balance $26.42 is visible."
                    ],
                },
            )


@pytest.mark.parametrize(
    "scenario",
    [
        "code",
        "duplicate",
        "cancel",
        "slow",
        "email",
        "email_interpretation",
        "email_fallback",
        "email_uninterpretable",
        "email_revoked",
        "email_delayed",
        "email_unconfirmed",
        "email_continue",
        "email_rejected",
    ],
)
async def test_gateway_checkout_continues_after_correlated_otp_reply(
    tmp_path, monkeypatch, scenario
):
    settings = checkout_settings(tmp_path)
    settings.browser.background.budget.transaction_commits = 2
    settings.authority.capabilities["browser_commit"].max_financial_limit_minor = 6000
    if scenario == "email_continue":
        settings.browser.background.budget.transaction_commits = 3
        settings.authority.capabilities["browser_commit"].max_financial_limit_minor = 9000
    if scenario.startswith("email"):
        profile = settings.profile_configs["personal"]
        profile.google = GoogleSettings(
            accounts={"mail": GoogleAccountSettings(email="owner@example.com")}
        )
        profile.google_oauth_clients = {
            "mail": GoogleOAuthClientSettings(
                client_id="fixture", client_secret=SecretStr("fixture")
            )
        }
        settings.browser.verification.enabled = True
        settings.browser.verification.allow_background = True
        settings.browser.verification.gmail_accounts = ["personal/mail"]

        async def read_email(_reader, query):
            assert query.source.account.qualified == "personal/mail"
            assert query.recipient == "owner@example.com"
            if scenario == "email_fallback":
                raise VerificationUnavailable("The authorized Gmail account needs reconnection.")
            if scenario == "email_revoked":
                request = (await ExecutionStore(settings).list(scope=_SCOPE, status="running"))[0]
                assert request.grant_id is not None
                await AuthorityStore(settings).revoke(
                    request.grant_id,
                    scope=_SCOPE,
                    actor="fixture-owner",
                    reason="Stop verification.",
                )
            code = "AB12CD" if scenario == "email_interpretation" else "123456"
            return (
                VerificationMessage(
                    account=query.source.account,
                    message_id="fixture-message",
                    received_at=datetime.now(UTC),
                    sender="verify@checkout.example",
                    recipients=("owner@example.com",),
                    subject="Verify your purchase",
                    text="Use your mobile authenticator to continue."
                    if scenario == "email_uninterpretable"
                    else f"Your verification code is {code}.",
                ),
            )

        monkeypatch.setattr(
            "ricky.tools.integrations.gmail.verification.GmailVerificationReader.read", read_email
        )
    if scenario == "slow":
        settings.agents.ad_hoc_background.execution.wall_clock_seconds = 30
    executable = shutil.which("google-chrome-stable")
    assert executable
    starts, purchases = [], []
    launch = BrowserType.launch_persistent_context

    async def fixture_launch(self, *args, **kwargs):
        context = await launch(self, *args, **kwargs)

        async def respond(route):
            request = route.request
            if not request.url.startswith(ORIGIN):
                await route.abort()
                return
            if request.method == "POST" and request.url.endswith("/verify"):
                expected_code = "AB12CD" if scenario == "email_interpretation" else "123456"
                assert parse_qs(request.post_data or "").get("otp") == [expected_code]
                if scenario == "email_continue":
                    body = (
                        b'<h1>Verification accepted</h1><form method="post" action="/finalize">'
                        b"<p>Credits $20.00; service fee $1.60; total $21.60</p>"
                        b"<button>Complete purchase</button></form>"
                    )
                elif scenario == "email_rejected":
                    body = b"<h1>Verification rejected; no purchase was made</h1>"
                elif scenario in {"email_delayed", "email_unconfirmed"}:
                    body = b"<h1>Processing verification</h1>"
                    if scenario == "email_delayed":
                        body += (
                            b"<script>setTimeout(()=>location.replace('/settled'),7500)</script>"
                        )
                else:
                    purchases.append("completed")
                    body = b"<h1>Purchase completed</h1><p>Credit balance: $26.42</p>"
            elif (
                request.url.endswith("/settled")
                or request.method == "POST"
                and request.url.endswith("/finalize")
            ):
                purchases.append("completed")
                body = b"<h1>Purchase completed</h1><p>Credit balance: $26.42</p>"
            elif request.method == "POST":
                starts.append("pending")
                body = b"""<h1>Verify payment</h1><form action="/verify" method="post">
<label>Email verification code<input autocomplete="one-time-code" name="otp"
oninput="if(this.value.length===6)this.form.requestSubmit()"></label></form>"""
            else:
                body = checkout_html()
            await route.fulfill(status=200, content_type="text/html", body=body)

        await context.route("**/*", respond)
        return context

    monkeypatch.setattr(BrowserType, "launch_persistent_context", fixture_launch)

    async def resolve(_host, _port):
        return ("93.184.216.34",)

    monkeypatch.setattr(
        "ricky.browser.service.DestinationPolicy",
        lambda **kw: DestinationPolicy(resolver=resolve, **kw),
    )
    monkeypatch.setattr(
        "ricky.runtime.composition.built_in_guardrail_evaluators", browser_guardrail_evaluators
    )
    monkeypatch.setattr(
        "ricky.authority.compiler.default_authority_registry",
        lambda: AuthorityRegistry(list(browser_authority_evaluators())),
    )

    async def browser_factory(config, **kwargs):
        return BrowserService(
            config, backend=PlaywrightBrowserBackend(), executable_path=Path(executable), **kwargs
        )

    @asynccontextmanager
    async def runtime_factory(*args, **kwargs):
        async with build_session_runtime(
            *args, background_browser_factory=browser_factory, **kwargs
        ) as runtime:
            yield runtime

    monkeypatch.setattr("ricky.jobs.runner.build_session_runtime", runtime_factory)
    foreground = ChallengeDelegator()
    coordinator = ConversationCoordinator(settings, provider_factory=lambda *_: foreground)
    message = await _ingest(
        settings,
        suffix="a",
        text="Check balance and purchase $20 credits if below $10; complete email verification.",
    )
    await coordinator.process(message.id)
    transport = HandoffTransport()
    messaging = _handoff_messaging(settings, transport)
    assert await messaging.deliver_once() == 1
    assert await coordinator.reconcile_handoffs() == 1
    contracts = await ExecutionStore(settings).list_contracts(scope=_SCOPE)
    assert len(contracts) == 1
    contract = contracts[0]
    assert ExecutionContract.model_validate_json(contract.model_dump_json()) == contract
    if scenario.startswith("email"):
        assert contract.version == 4 and contract.verification is not None
        assert [s.account.qualified for s in contract.verification.sources] == ["personal/mail"]
        legacy_values = contract.model_dump(mode="json", exclude={"digest", "verification"})
        legacy_values["version"] = 3
        legacy = build_execution_contract(**legacy_values)
        assert legacy.verification is None
        historical = legacy.model_dump(mode="json", exclude={"verification"})
        assert ExecutionContract.model_validate_json(json.dumps(historical)) == legacy
    else:
        assert contract.version == 3 and contract.verification is None
    routes = RoutePolicy(settings, conversation_resolver=GatewayStore(settings))
    dispatcher = ExecutionDispatcher(
        settings,
        project_root=tmp_path,
        store=ExecutionStore(settings),
        provider_factory=lambda _: ChallengeWorker(scenario),
        authority_registry=AuthorityRegistry(list(browser_authority_evaluators())),
        routes=routes,
        notifications=NotificationService(settings, routes=routes),
    )
    coordinator.bind_dispatcher(dispatcher)
    approvals = []
    notify = dispatcher.notify_browser_approval

    async def approve(challenge, *, scope):
        assert purchases == []
        await notify(challenge, scope=scope)
        await messaging.deliver_once()
        command = re.search(r"/approve browser_\S+ \S+", transport.sent[-1].text)
        assert command
        approvals.append(challenge.approval.id)
        message = await _ingest(settings, suffix="bdef"[len(approvals) - 1], text=command.group())
        await coordinator.process(message.id)

    monkeypatch.setattr(dispatcher, "notify_browser_approval", approve)
    notify_code = dispatcher._request_browser_challenge
    cancellation_ready = asyncio.Event()
    cancellation_request = []

    async def cancel_from_gateway():
        await cancellation_ready.wait()
        message = await _ingest(settings, suffix="c", text=f"/cancel {cancellation_request[0]}")
        await coordinator.process(message.id)

    async def supply_code(owner, request_id, principal_id, scope):
        assert scenario not in {"email", "email_interpretation", "email_revoked"}
        if scenario == "email_fallback":
            assert owner.assistance_reason == "The authorized Gmail account needs reconnection."
        if scenario == "email_uninterpretable":
            assert (
                owner.assistance_reason
                == "The verification email did not contain an identifiable code."
            )
        await notify_code(owner, request_id, principal_id, scope)
        await messaging.deliver_once()
        assert starts == ["pending"] and purchases == []
        if scenario == "cancel":
            cancellation_request.append(request_id)
            cancellation_ready.set()
            return
        if scenario == "slow":
            # The user alone waits longer than the complete active-work budget.
            # The job must retain its live browser and resume within wait capacity.
            await asyncio.sleep(31)
        message = await _ingest(
            settings, suffix="c", text="123456", reply_to=str(len(transport.sent))
        )
        await coordinator.process(message.id)
        if scenario == "duplicate":
            second = await _ingest(
                settings, suffix="e", text="123456", reply_to=message.reply_to_platform_message_id
            )
            await coordinator.process(second.id)

    monkeypatch.setattr(dispatcher, "_request_browser_challenge", supply_code)
    cancellation = asyncio.create_task(cancel_from_gateway()) if scenario == "cancel" else None
    try:
        completed = await dispatcher.worker_once(scope=_SCOPE)
        if cancellation is not None:
            await cancellation
    finally:
        if cancellation is not None and not cancellation.done():
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)
    if scenario == "cancel":
        assert len(completed) == 1 and completed[0].status in {"uncertain", "cancelled"}
        assert starts == ["pending"] and purchases == []
        assert len(approvals) == 1
        return
    if scenario == "email_revoked":
        assert len(completed) == 1 and completed[0].status in {"failed", "uncertain"}
        assert starts == ["pending"] and purchases == []
        assert len(approvals) == 1
        records = await BrowserChallengeStore(settings).list(scope=_SCOPE)
        assert len(records) == 1 and records[0].state == "invalidated"
        return
    if scenario == "email_unconfirmed":
        assert len(completed) == 1 and completed[0].status == "uncertain"
        assert starts == ["pending"] and purchases == []
        assert len(approvals) == 2
        return
    if scenario == "email_rejected":
        assert len(completed) == 1 and completed[0].status == "failed"
        assert completed[0].error is not None and "Verification rejected" in completed[0].error
        assert starts == ["pending"] and purchases == []
        assert len(approvals) == 2
        return
    assert len(completed) == 1 and completed[0].status == "succeeded", [
        (r.status, r.error) for r in completed
    ]
    assert starts == ["pending"] and purchases == ["completed"]
    assert len(approvals) == (3 if scenario == "email_continue" else 2)
    assert await dispatcher.worker_once(scope=_SCOPE) == []
