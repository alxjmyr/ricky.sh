"""Real Chrome checkout through gateway delegation, review, and effect receipts."""

from __future__ import annotations

import json
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from playwright.async_api import BrowserType

from browser_transaction_support import checkout_html
from ricky.authority.registry import AuthorityRegistry
from ricky.browser.authority import browser_authority_evaluators
from ricky.browser.guardrails import browser_guardrail_evaluators
from ricky.browser.playwright_backend import PlaywrightBrowserBackend
from ricky.browser.policy import DestinationPolicy
from ricky.browser.service import BrowserService
from ricky.config import RickySettings
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.executions.store import ExecutionStore
from ricky.gateway.conversations import ConversationCoordinator
from ricky.gateway.store import GatewayStore
from ricky.jobs.store import JobRunStore
from ricky.notifications.routes import RoutePolicy
from ricky.notifications.service import NotificationService
from ricky.runtime import build_session_runtime
from test_gateway_conversations import (
    _SCOPE,
    AdHocProvider,
    HandoffTransport,
    _answer,
    _handoff_messaging,
    _ingest,
    _settings,
    _tool,
)

pytestmark = pytest.mark.browser_integration
ORIGIN = "https://checkout.example"


def checkout_settings(tmp_path: Path) -> RickySettings:
    raw = _settings(tmp_path).model_dump(mode="python")
    raw["project_data_dir"] = str(tmp_path / "project")
    raw["browser"] = {
        "enabled": True,
        "background": {
            "enabled": True,
            "read_enabled": True,
            "interaction_enabled": True,
            "commit_enabled": True,
            "budget": {"transaction_commits": 1},
        },
    }
    raw["profile_configs"] = {
        "personal": {
            "browser": {
                "resources": {
                    "checkout": {
                        "kind": "persistent",
                        "headless": True,
                        "description": "Synthetic checkout",
                    },
                }
            }
        }
    }
    raw["authority"] = {
        "enabled": True,
        "allowed_principals": ["telegram:personal/bot:100"],
        "max_effect_calls": 20,
        "capabilities": {
            "browser_interact": {
                "enabled": True,
                "allowed_profiles": ["shared", "personal"],
                "max_effect_calls": 20,
            },
            "browser_commit": {
                "enabled": True,
                "allowed_profiles": ["shared", "personal"],
                "max_effect_calls": 20,
                "max_financial_limit_minor": 3000,
                "currency": "USD",
            },
        },
    }
    raw["agents"] = {
        "ad_hoc_background": {
            "confirmation_required_capabilities": [],
            "guardrail_required_capabilities": [],
            "execution": {"effect_calls": 20, "iterations": 30},
        }
    }
    return RickySettings.model_validate(raw)


class CheckoutDelegator(AdHocProvider):
    async def stream(self, request):
        if self.step != 1:
            async for event in super().stream(request):
                yield event
            return
        self.requests.append(request)
        self.step += 1
        results = "\n".join(
            p.content for m in request.messages for p in m.content if p.kind == "tool_result"
        )
        task = re.search(r"task_[0-9a-f]{32}", results)
        assert task is not None
        tools = {
            "builtin.browser.read": "browser_navigate,browser_snapshot",
            "builtin.browser.interact": "browser_session_open_resource,browser_click,browser_fill",
            "builtin.browser.commit": "browser_commit",
        }
        yield _tool(
            "delegate",
            "delegate_task",
            {
                "action": "start",
                "task_id": task.group(),
                "expected_task_revision": 1,
                "goal": "Check balance. If below $10 buy $20 credits with fees within $30. "
                "Report starting and ending balance.",
                "requested_capabilities": list(tools),
                "guardrails": [
                    {
                        "capability_id": cap,
                        "fields": [
                            {"field": "mode", "value": "transaction"},
                            {"field": "allowed_tools", "value": names},
                            {"field": "resources", "value": "personal/checkout"},
                            {
                                "field": "authenticated_origins",
                                "value": f"personal/checkout#{ORIGIN}",
                            },
                        ],
                    }
                    for cap, names in tools.items()
                ],
            },
        )


class CheckoutWorker(AdHocProvider):
    """Script the task, using only actual model-visible observations for target ids."""

    async def stream(self, request):
        self.requests.append(request)
        self.step += 1
        results = [
            p.content for m in request.messages for p in m.content if p.kind == "tool_result"
        ]
        if self.step == 1:
            yield _tool("open", "browser_session_open_resource", {"resource": "personal/checkout"})
            return
        session = re.search(r"browser_session_[0-9a-f]{32}", "\n".join(results))
        assert session is not None
        if self.step == 2:
            yield _tool(
                "navigate", "browser_navigate", {"session_id": session.group(), "url": ORIGIN}
            )
        elif self.step in {3, 5, 7, 9}:
            yield _tool(
                f"snapshot-{self.step}", "browser_snapshot", {"session_id": session.group()}
            )
        elif self.step in {4, 6, 8}:
            latest = results[-1]
            name = {4: "Open checkout", 6: "Credit amount (USD)", 8: "Purchase credits"}[self.step]
            controls = [
                json.loads(line)
                for line in latest.splitlines()
                if line.startswith("{") and '"control_kind"' in line
            ]
            control = next(c for c in controls if c.get("name") == name)
            snapshot = re.search(r"browser_snapshot_[0-9a-f]{32}", latest)
            page = re.search(r"browser_page_[0-9a-f]{32}", latest)
            assert snapshot is not None and page is not None
            args: dict[str, object] = {
                "target": {
                    "session_id": session.group(),
                    "page_id": page.group(),
                    "snapshot_id": snapshot.group(),
                    "ref": control["ref"],
                }
            }
            if self.step == 6:
                args["value"] = "20"
            if self.step == 8:
                assert "21.60" in latest
                args["envelope"] = {
                    "kind": "financial",
                    "intent": "Buy $20 credits",
                    "payee": "Fixture merchant",
                    "total": {"amount": "21.60", "currency": "USD"},
                    "components": [
                        {"label": "Credits", "amount": {"amount": "20.00", "currency": "USD"}}
                    ],
                    "fees": [
                        {"label": "Service fee", "amount": {"amount": "1.60", "currency": "USD"}}
                    ],
                    "timing": "one_time",
                    "source": {"kind": "site", "label": "Saved account"},
                    "consequences": ["One-time charge of $21.60"],
                    "expected_result": "Credit balance becomes $26.42",
                }
            yield _tool(
                f"act-{self.step}",
                {4: "browser_click", 6: "browser_fill", 8: "browser_commit"}[self.step],
                args,
            )
        elif self.step == 10:
            assert "26.42" in results[-1]
            yield _tool(
                "outcome",
                "report_task_outcome",
                {
                    "status": "completed",
                    "summary": "Credit purchase completed.",
                    "evidence": ["The resulting page shows credit balance $26.42."],
                },
            )
        else:
            yield _answer("Starting balance: $6.42. Charged: $21.60. Ending balance: $26.42.")


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
