"""Synthetic real-Chrome OTP submission through the prepared commit boundary."""

import asyncio
import shutil
from pathlib import Path

import pytest
from playwright.async_api import BrowserType
from pydantic import SecretStr

from browser_checkout_support import checkout_settings
from ricky.agent import AgentSession
from ricky.browser.challenge_store import BrowserChallengeStore
from ricky.browser.challenge_wait import ChallengeWaitBudget
from ricky.browser.challenges import ChallengeError, ChallengeResponse, ChallengeSource
from ricky.browser.playwright_backend import PlaywrightBrowserBackend
from ricky.browser.policy import DestinationPolicy
from ricky.browser.service import BrowserService
from ricky.browser.tools import (
    BrowserChallengeParams,
    BrowserChallengeTool,
    BrowserCommitParams,
    BrowserCommitTool,
)
from ricky.browser.types import BrowserActionTarget, BrowserError
from ricky.tools import ToolContext

pytestmark = pytest.mark.browser_integration


@pytest.mark.parametrize(
    "scenario", ["otp", "cancel", "close", "manual_done", "manual_unchanged", "expired"]
)
async def test_real_chrome_otp_is_prepared_then_submitted_once(tmp_path, monkeypatch, scenario):
    executable = shutil.which("google-chrome-stable")
    assert executable
    settings = checkout_settings(tmp_path)
    scope = settings.resolve_profile_scope("personal")
    submissions = []
    contexts = []
    launch = BrowserType.launch_persistent_context

    async def fixture_launch(self, *args, **kwargs):
        context = await launch(self, *args, **kwargs)
        contexts.append(context)

        async def respond(route):
            if route.request.url.startswith("https://verify.example"):
                if route.request.method == "POST":
                    submissions.append("submitted")
                    body = "<h1>Account verified</h1>"
                else:
                    body = """<form action="/finish" method="post">
<label>Verification code<input name="otp" autocomplete="one-time-code"
 oninput="if(this.value.length===6)this.form.requestSubmit()"></label>
</form>"""
                await route.fulfill(status=200, content_type="text/html", body=body)
            else:
                await route.abort()

        await context.route("**/*", respond)
        return context

    monkeypatch.setattr(BrowserType, "launch_persistent_context", fixture_launch)

    async def resolve(_host, _port):
        return ("93.184.216.34",)

    monkeypatch.setattr(
        "ricky.browser.service.DestinationPolicy",
        lambda **kw: DestinationPolicy(resolver=resolve, **kw),
    )
    service = BrowserService(
        settings,
        scope=scope,
        backend=PlaywrightBrowserBackend(),
        executable_path=Path(executable),
        challenge_wait=ChallengeWaitBudget(0.1) if scenario == "expired" else None,
    )
    try:
        session = await service.open_resource("personal/checkout")
        await service.navigate(session.session_id, page_id=None, url="https://verify.example")
        snapshot = await service.snapshot(session.session_id, page_id=None)
        descriptor = next(
            item for item in snapshot.descriptors if item.protected_kind == "one_time_code"
        )
        target = BrowserActionTarget(
            session_id=session.session_id,
            page_id=snapshot.page.page_id,
            snapshot_id=snapshot.snapshot_id,
            ref=descriptor.ref,
        )
        ctx = ToolContext(
            cwd=tmp_path,
            settings=settings,
            session=AgentSession.create(
                settings, profile_scope=scope, provider="openrouter", model="test"
            ),
        )

        async def responder(owner):
            if scenario == "expired":
                assert (owner.record.expires_at - owner.record.created_at).total_seconds() <= 0.1
                await asyncio.sleep(1)
                pytest.fail("expired wait must cancel its responder")
            source = ChallengeSource(
                principal_id="cli", conversation_id="test", prompt_message_id=owner.record.id
            )
            await owner.bind_source(source)
            if scenario == "manual_done":
                await (
                    contexts[0]
                    .pages[0]
                    .evaluate("document.body.innerHTML='<h1>Device verified</h1>'")
                )
            await owner.respond(
                ChallengeResponse(
                    code=SecretStr("123456") if scenario in {"otp", "cancel", "close"} else None
                ),
                source=source,
            )

        requested = await BrowserChallengeTool(service, responder).run(
            BrowserChallengeParams(
                target=target,
                instruction="Enter the code.",
                purpose="authentication",
                kind="manual" if scenario.startswith("manual") else "otp",
            ),
            ctx,
        )
        if scenario == "expired":
            assert requested.is_error
            records = await BrowserChallengeStore(settings).list(scope=scope)
            assert len(records) == 1 and records[0].state == "expired"
            assert submissions == []
            return
        if scenario != "otp":
            assert requested.is_error == (scenario == "manual_unchanged")
            assert submissions == []
            return
        assert not requested.is_error
        assert isinstance(requested.data, dict)
        params = BrowserCommitParams.model_validate(
            {
                "target": target,
                "activation": "challenge",
                "challenge_id": requested.data["challenge_id"],
                "envelope": {
                    "kind": "browser",
                    "intent": "Complete account verification",
                    "destination": "https://verify.example",
                    "consequences": ["Authenticate this account"],
                    "disclosures": ["One-time verification response"],
                    "expected_result": "Account verified",
                },
            }
        )
        tool = BrowserCommitTool(service)
        if scenario == "close":
            await service.close_session(session.session_id)
            records = await BrowserChallengeStore(settings).list(scope=scope)
            assert len(records) == 1 and records[0].state == "invalidated"
            with pytest.raises(BrowserError):
                await tool.prepare_effect(params.model_dump(mode="python"), ctx)
            assert submissions == []
            return
        if scenario == "cancel":
            with pytest.raises(BrowserError, match="pending code"):
                await service.navigate(
                    session.session_id, page_id=None, url="https://verify.example"
                )
            cancelled = await BrowserChallengeTool(service, responder).run(
                BrowserChallengeParams(
                    target=target,
                    instruction="Replace the expired code.",
                    purpose="authentication",
                    cancel_challenge_id=params.challenge_id,
                ),
                ctx,
            )
            assert not cancelled.is_error
            with pytest.raises(ChallengeError, match="unavailable"):
                await tool.prepare_effect(params.model_dump(mode="python"), ctx)
            await service.navigate(session.session_id, page_id=None, url="https://verify.example")
            assert submissions == []
            return
        prepared = await tool.prepare_effect(params.model_dump(mode="python"), ctx)
        assert submissions == []
        assert "123456" not in prepared.permission_summary
        result = await tool.run_prepared(params, prepared, ctx)
        assert not result.is_error, result.content
        assert submissions == ["submitted"]
        observed = await service.snapshot(session.session_id, page_id=None)
        assert "Account verified" in observed.content
        with pytest.raises(ChallengeError, match="unavailable"):
            await tool.run_prepared(params, prepared, ctx)
        assert submissions == ["submitted"]
    finally:
        await service.aclose()
