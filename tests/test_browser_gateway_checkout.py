"""Real Chrome checkout through gateway delegation, review, and effect receipts."""

from __future__ import annotations

import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from playwright.async_api import BrowserType

from browser_checkout_support import (
    ORIGIN,
    CheckoutDelegator,
    CheckoutWorker,
    checkout_settings,
)
from browser_transaction_support import checkout_html
from gateway_conversation_support import (
    _SCOPE,
    HandoffTransport,
    _handoff_messaging,
    _ingest,
)
from ricky.authority.registry import AuthorityRegistry
from ricky.browser.authority import browser_authority_evaluators
from ricky.browser.guardrails import browser_guardrail_evaluators
from ricky.browser.playwright_backend import PlaywrightBrowserBackend
from ricky.browser.policy import DestinationPolicy
from ricky.browser.service import BrowserService
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.executions.store import ExecutionStore
from ricky.gateway.conversations import ConversationCoordinator
from ricky.gateway.store import GatewayStore
from ricky.jobs.store import JobRunStore
from ricky.notifications.routes import RoutePolicy
from ricky.notifications.service import NotificationService
from ricky.runtime import build_session_runtime

pytestmark = pytest.mark.browser_integration


async def test_gateway_real_checkout_approves_rendered_command_once(tmp_path, monkeypatch):
    executable = shutil.which("google-chrome-stable")
    assert executable, "Google Chrome Stable is required"
    settings = checkout_settings(tmp_path)
    submissions = []
    launch = BrowserType.launch_persistent_context

    async def fixture_launch(self, *args, **kwargs):
        context = await launch(self, *args, **kwargs)

        async def respond(route):
            request = route.request
            if request.url.startswith(ORIGIN):
                if request.method == "POST":
                    submissions.append(parse_qs(request.post_data or ""))
                    body = b"<h1>Purchase completed</h1><p>Credit balance: $26.42</p>"
                else:
                    body = checkout_html()
                await route.fulfill(status=200, content_type="text/html", body=body)
            else:
                await route.abort()

        await context.route("**/*", respond)
        return context

    monkeypatch.setattr(BrowserType, "launch_persistent_context", fixture_launch)

    async def resolve(host, port):
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

    async def browser_factory(settings, **kwargs):
        return BrowserService(
            settings, backend=PlaywrightBrowserBackend(), executable_path=Path(executable), **kwargs
        )

    @asynccontextmanager
    async def runtime_factory(*args, **kwargs):
        async with build_session_runtime(
            *args, background_browser_factory=browser_factory, **kwargs
        ) as runtime:
            yield runtime

    monkeypatch.setattr("ricky.jobs.runner.build_session_runtime", runtime_factory)
    foreground = CheckoutDelegator()
    coordinator = ConversationCoordinator(settings, provider_factory=lambda *_: foreground)
    inbound = await _ingest(
        settings,
        suffix="a",
        text="Check balance and buy $20 credits if below $10. Maximum charge $30.",
    )
    await coordinator.process(inbound.id)
    store = ExecutionStore(settings)
    queued = await store.list(scope=_SCOPE, limit=10)
    assert len(queued) == 1 and queued[0].status == "awaiting_acknowledgement", [
        p.content
        for r in foreground.requests
        for m in r.messages
        for p in m.content
        if p.kind == "tool_result"
    ]
    transport = HandoffTransport()
    messaging = _handoff_messaging(settings, transport)
    assert await messaging.deliver_once() == 1
    assert await coordinator.reconcile_handoffs() == 1
    worker = CheckoutWorker()
    routes = RoutePolicy(settings, conversation_resolver=GatewayStore(settings))
    dispatcher = ExecutionDispatcher(
        settings,
        project_root=tmp_path,
        store=store,
        provider_factory=lambda _: worker,
        authority_registry=AuthorityRegistry(list(browser_authority_evaluators())),
        routes=routes,
        notifications=NotificationService(settings, routes=routes),
    )
    coordinator.bind_dispatcher(dispatcher)
    notify = dispatcher.notify_browser_approval
    approvals = []

    async def approve(challenge, *, scope):
        assert submissions == []
        assert challenge.approval.envelope.total.amount == "21.60"
        await notify(challenge, scope=scope)
        await messaging.deliver_once()
        rendered = transport.sent[-1].text
        assert "Service fee" in rendered and "21.60 USD" in rendered
        assert "sha256" not in rendered and "session_starts" not in rendered
        command = re.search(r"/approve browser_\S+ \S+", rendered)
        assert command is not None
        # Use the command actually delivered to the user, never the challenge secret.
        approvals.append(challenge.approval.id)
        inbound = await _ingest(settings, suffix="b", text=command.group())
        await coordinator.process(inbound.id)

    monkeypatch.setattr(dispatcher, "notify_browser_approval", approve)
    completed = await dispatcher.worker_once(scope=_SCOPE)
    assert len(completed) == 1
    assert completed[0].status == "succeeded", completed[0].error
    assert submissions == [{"credits": ["20"], "charged": ["21.60"]}]
    assert len(approvals) == 1
    stored = await store.browser_approvals_for_request(completed[0].id, scope=_SCOPE)
    assert stored[0].state == "consumed"
    assert completed[0].run_id is not None
    receipts = await JobRunStore(settings).actions_for_run(completed[0].run_id, scope=_SCOPE)
    assert [r.status for r in receipts] == ["performed"] * 3
    await messaging.deliver_once()
    assert "$26.42" in transport.sent[-1].text and "$6.42" in transport.sent[-1].text
    assert await dispatcher.worker_once(scope=_SCOPE) == []
    assert len(submissions) == 1
